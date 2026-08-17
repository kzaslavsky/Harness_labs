# Self-Improvement Agent — Engineering Plan

Status: draft (not yet implemented)

## 0. Grounding: this system already exists, done by hand

[`docs/development/contract-burden-reduction.md`](contract-burden-reduction.md) **is** a
manually executed instance of the loop this plan mechanizes: a living
diagnosis mined from real journals (`logs/runs/orbit-graph-orbit-exp-{1,2}/`,
plus a Retinology FR-20 audit of 232 run directories across 101 graph
attempts), each item citing run evidence, classifying the fix, and recording
a landing commit (`landed (CB-04, commit d8e5e8e…)`). It carries an explicit
generalizability rubric — the **admission test for relaxing a gate**: a gate
qualifies only when (1) *defeated by mechanical compliance* (a run satisfied
it with a semantics-free transformation) or (2) *superseded* by a stronger
mechanism. Companion prose corpora:
[`plangraph-parallelization-run-defect-and-retry-postmortem.md`](plangraph-parallelization-run-defect-and-retry-postmortem.md),
[`implement-v13-codex-failure-analysis.md`](implement-v13-codex-failure-analysis.md),
[`recovery-machinery-inventory.md`](recovery-machinery-inventory.md).

Design consequence: this is not a greenfield pattern-miner. It is
**mechanization of a documented human workflow that has already produced 11
landed policy changes**, and that workflow's rubric, evidence standard, and
status vocabulary should be lifted verbatim rather than reinvented.
`scripts/dev/check_workaround_retirement.py` is the existing deterministic
gate proving closure of that worklist — the shape the loop's "did it land
and stay landed" check should take.

## 1. Architecture and layer placement

Boundaries are hard-enforced by `scripts/dev/check_import_boundaries.py`
(AST walk incl. in-function imports) plus `tests/test_import_boundaries.py`,
with `ALLOWED` = `core→core`, `featurerun→core+self`,
`plangraph→core+featurerun+self`, `observability→core+self`,
`graphrun→everything`.

The component is **two pieces in two existing layers, not one new
package**:

**(a) Mining/analysis → `harness_labs/observability/`.** New modules
`run_forensics.py` (per-run blocker/recovery extraction) and
`improvement_index.py` (cross-run clustering). This is precisely what the
layer is for: `run_catalog.py`'s docstring already licenses data-coupling to
featurerun/plangraph journal event shapes while importing core only ("the
catalog's job is reading every harness's journals"). Reuse
`project_run_metrics()` (`observability/run_metrics.py`) — it refuses to
project a run whose hash chain doesn't verify
(`AuditJournal.verify_checkpoint_prefix`), giving authenticated-input-only
for free — and `build_run_metrics_index()` / `merge_run_catalogs()` for
batch and multi-root corpora.

**(b) Proposal + gate + dispatch surface → `harness_labs/graphrun/`.** New
`improvement_program.py`. Drafting a proposal, issuing an approval receipt,
and dispatching the implementing FeatureRun requires
`plangraph.plan_approval` + `featurerun.feature_run` + observability
together — legal only in `graphrun` ("composition + operator surface;
nothing imports it"). Zero edits to the boundary checker required.

**Rejected: a new top-level `selfimprove/` layer.** It would require editing
`LAYERS`, `ALLOWED`, `FUTURE_LAYERS` in the checker plus
`tests/test_import_boundaries.py`, and would be a sibling of `graphrun` with
identical import rights — a second "may import everything" layer, weakening
the invariant "nothing imports graphrun." Revisit only if the improvement
surface outgrows one module.

**CLI**: `scripts/self_improve.py` with subcommands
`mine | cluster | propose | prepare | issue | monitor`, mirroring
`scripts/approve_plan.py`'s `prepare`/`issue` split (thin argparse over the
library, JSON to stdout, errors to stderr, exit 1). Mining state and
unapproved drafts live under `logs/improvement/`, gitignored alongside the
existing `logs/plan-approval/` and `logs/registration/` entries in
`.gitignore`.

**What is committed vs. local.** `logs/runs/*` is gitignored local evidence
and stays that way. The **pattern and proposal records are the exportable,
committed artifacts** — sanitized, hash-referencing the local journals they
came from. That is the only way a corpus survives ephemeral checkouts and
machine changes, and it satisfies "no required contract may exist only in
memory."

## 2. Data model (new schemas in `schemas/`)

Style follows the repo's two existing idioms: `protocol:
{"const": "…/1"}` + `additionalProperties: false` (e.g.
`plan-approval-receipt.schema.json`, `run-descriptor.schema.json`) and the
`decision.schema.json` field vocabulary. **Reuse existing enums verbatim** —
do not mint new taxonomies:

- failure classification: `product | indeterminate |
  infrastructure_transient | harness_or_configuration | policy_violation |
  structural_decision` (from `schemas/block-escalation.json`,
  `schemas/retry-budget-ledger.json`, produced by
  `classify_verification_failure()` in
  `harness_labs/featurerun/feature_run.py` with stable `rule_id`s like
  `timeout-exit-124`, `driver-crash-pytest-green`).
- `evidence_classification`: `production_lifecycle | component | synthetic
  | fabricated_fixture` (`schemas/audit-event.schema.json`).
- finding identity `(file, subject)` and `reopened_count` /
  `fix_attempts` / `outcome` (`schemas/review-ledger.schema.json`).

### `blocker-observation/1` — one blocker instance in one run

`protocol`, `run_id`, `run_kind`, `evidence_classification`,
`node_id|null`, `attempt_id`, `phase` (lifecycle enum), `classification`
(above), `rule_id|null`, `signature` (normalized, secret-free failure key —
e.g. rejection message with ids stripped, or `failing_identifiers` set from
`plangraph/plan_graph_budget.py`), `first_event_sequence`,
`event_hashes[]` (chain anchors — cheap tamper evidence), `resolution` ∈
`{self_recovered, repair_attempt, retry_renewed, operator_intervention,
prompt_workaround, transferred, unresolved_blocked}`, `resolution_cost`
(`{retries, repair_dispatches, wall_clock_ms, tokens,
diff_churn_lines}`), `artifact_refs[]` (`{path, sha256}` as in
`run-event.schema.json`), `redaction_applied: bool`.

### `blocker-pattern/1` — a cluster across runs

`protocol`, `pattern_id`, `signature`, `classification`, `status` ∈
`{observed, candidate, proposed, addressed, superseded, rejected}`,
`support` (`{observation_count, distinct_run_count, distinct_lineage_count,
distinct_task_suite_count, distinct_harness_versions}`), `first_seen_at` /
`last_seen_at`, `observations[]` (refs), `cost_aggregate` (median + tail,
per `docs/observability/logging-and-metrics.md`), `fixes_employed[]`
(`{mechanism, count, succeeded, mean_cost}` — where `mechanism`
distinguishes in-harness recovery from **prompt-space compensation**, the
key generalizability tell), `generalizability` (`{verdict ∈ {one_off,
policy_gap, gate_defeated_by_mechanical_compliance,
superseded_by_existing_mechanism, environmental}, rubric_id:
"burden-admission/1", rationale, counterexamples[]}`),
`evidence_classification_filter` (must be `production_lifecycle`).

### `improvement-proposal/1` — the static-plane change

Mirrors `decision.schema.json`'s reasoning fields plus the **Complexity
admission** triple mandated by
`docs/architecture/harness-contract.md` §Complexity admission:
`demonstrated_failure`, `production_consumer`, `end_to_end_assertion` — all
three **required**, so an unfalsifiable proposal cannot serialize. Plus:
`proposal_id`, `pattern_ids[]`, `question/choice/alternatives/rationale
/evidence/consequences/reversible/status` (decision vocabulary),
`target_surface[]` (`{path, kind ∈ {doc, schema, gate, policy, code}}` —
with `AGENTS.md` and `docs/architecture/harness-contract.md` flagged
`requires_elevated_attestation`), `red_green` (`{base_commit,
finding_tests[], regression_targets[]}` feeding
`scripts/dev/red_green_check.py`), `accuracy_risk` ∈ `{none,
gate_relaxation, gate_addition}`, `success_criteria` (the measurable
before/after prediction: metric, direction, magnitude, task-suite id),
`rollback` (what reverts it), `proposed_decomposition_path|null` (a
`plan-graph-plan/1` per `schemas/plan-graph-plan.schema.json`).

### `improvement-approval-receipt/1` + `improvement-subject/1` + `improvement-operator-approval/1`

Structural clones of `plan-approval-{receipt,subject,gates}.schema.json`
and `plan-operator-approval.schema.json`, with `policy_id:
"operator-attested-harness-self-modification/1"`. Subject freezes:
repository identity (`.harness/repository.json`), `base_commit`, the
proposal blob + its git blob hash, every cited pattern record, and the
exact verification commands/timeouts. Receipt binds
`subject`/`gate_evidence`/`operator_approval` artifacts by sha256 — same
three-artifact shape `harness_labs/plangraph/plan_approval.py` already
implements, so `prepare_approval`/`issue_receipt` can be generalized rather
than duplicated.

### `improvement-outcome/1` — close the loop

`proposal_id`, `landed_commit`, `before_cohort[]` / `after_cohort[]` (run
ids), `task_suite_id` + `task_suite_version`, `comparability` (model/tool
versions, budgets, config — the logging contract's §Experiment discipline
list), `accuracy` (before/after with denominators), `efficiency`,
`composite`, `pattern_recurrence` (did the signature reappear?), `verdict`
∈ `{confirmed, inconclusive, regressed, reverted}`,
`decision_record_path` (the promoted `docs/decisions/NNNN-*.md`).

## 3. Pipeline stages

1. **Mine** (deterministic, read-only, no model). For each
   `logs/runs/<run-id>/` that verifies: read `events.jsonl` (`event_type`
   incl. `retry`, `status ∈ {failed, blocked}`, `phase ∈ {blocked, failed,
   recovering}`), `decisions.jsonl`, `checkpoint.json`, `summary.json`
   (`harness-run-summary/1` usage block), `manifest.json`, and artifacts —
   `review-ledger/1` (findings with `reopened_count > 0`, `fix_attempts`,
   `transferred`), `retry-budget-ledger/1` (`abandoned`/`blocked`/
   `extended`/`gate_changed` events, `failure_keys`),
   `plan-graph-block-escalation/1` (`decision_request`, `classification`),
   `workspace-change-receipt/2`. Emit `blocker-observation/1` records.
   **Hard filter: drop anything not `production_lifecycle`** — the logging
   contract forbids aggregating synthetic/fabricated evidence with
   production completion. Idempotent, watermarked by run-id; re-running
   mines only new dirs.
2. **Cluster/generalize** (deterministic grouping + bounded model
   judgment). Group by `signature` + `classification`. Only the *verdict*
   step uses a model, and its input is the record set, not raw
   transcripts. Applies the `burden-admission/1` rubric and thresholds
   (§5).
3. **Draft** (model). One `improvement-proposal/1` per qualifying pattern,
   including a `plan-graph-plan/1` decomposition when the change is more
   than a doc edit. Refuses to emit if the Complexity-admission triple
   can't be filled.
4. **Gate.** `self_improve.py prepare` → subject + deterministic gate
   evidence; operator signs `improvement-operator-approval/1`; `issue` →
   receipt. **No receipt, no implementation.** Same admission wall as
   `run_plan_graph.py run --approval-receipt`.
5. **Implement as a normal FeatureRun/PlanGraph against this repo**
   (dogfooding), under the receipt, in an isolated worktree/branch, with
   `scripts/dev/red_green_check.py` as the finding-test gate and
   `python3 scripts/check_repository_contracts.py` +
   `python3 -m pytest tests/ -q` as regression. The change lands as an
   ordinary reviewed commit touching `docs/`, `schemas/`, and code
   together.
6. **Monitor.** After ≥ N post-landing runs on the same task suite, emit
   `improvement-outcome/1`. Accuracy non-regression is a **hard gate**;
   efficiency gain is the objective. Verdict `regressed` triggers a revert
   proposal, not a quiet edit.
7. **Close.** Promote a confirmed outcome to `docs/decisions/NNNN-*.md` via
   `docs/decisions/TEMPLATE.md` (its "Validation and reversal" section is
   the outcome record's home), add it to `docs/decisions/README.md`'s
   accepted list, and mark the pattern `addressed`. A per-item retirement
   check in the `check_workaround_retirement.py` style proves the
   workaround it replaced is actually gone.

## 4. Recurrence mechanism

**Key constraint:** `logs/runs/*` is gitignored, local, mode-0700 evidence.
**CI cannot mine it** — a scheduled GitHub Action sees an empty corpus.
Recurrence must run where the journals live.

Recommended, in priority order:

1. **Run-completion trigger (primary).** Every finalized run already writes
   `manifest.json`; an operator-owned launcher wrapper calls
   `scripts/self_improve.py mine` after each run. Cheap (deterministic, no
   model), idempotent, and makes "after every N runs" natural: mining
   always runs; **clustering/proposal only fires when the watermark shows
   ≥ N new production runs since the last cycle**.
2. **Local scheduled cycle (secondary).** `launchd`/`cron` nightly on the
   operator's machine invoking `self_improve.py cluster
   --propose-if-ready`. Purely additive.
3. **CI's real job (complementary).** A workflow that validates
   *committed* improvement artifacts: schema validity,
   `check_repository_contracts.py`, `check_import_boundaries.py`,
   red/green evidence, and that every `status: accepted` proposal has a
   receipt and an outcome record.
4. **Session-level automation (optional).** A recurring Claude Code Routine
   / `/loop` can wake an agent session to run step 2 and draft proposals.
   Treat it as a convenience trigger only — the durable contract stays the
   repo-owned script, per "no required contract may exist only in a
   prompt."

## 5. Guardrails

- **Read-only miner.** The mining/clustering agent gets no writable-path
  grant beyond `logs/improvement/`; the deny-by-default capability broker
  (`docs/architecture/capability-brokers.md`, `core/capability_broker.py`)
  enforces it. It cannot touch `docs/`, `schemas/`, `AGENTS.md`, or code.
  Ever.
- **Approval receipt is the only door.** Implementation dispatch refuses
  without a valid `improvement-approval-receipt/1` binding the exact base
  commit, proposal digest, scope, and verification commands — same
  fail-closed structure as `plan_approval.py` (`_validate_receipt_shape`,
  `_revalidate_host_executables`).
- **Elevated attestation for the constitution.** Proposals targeting
  `AGENTS.md`, `docs/architecture/harness-contract.md`, or any accuracy
  gate carry `accuracy_risk: gate_relaxation` and require a distinct
  operator statement naming the gate and the superseding mechanism — the
  `burden-admission/1` rubric's clause 2, mechanized.
- **Accuracy is a constraint, never a trade.** `improvement-outcome/1`
  computes the composite only within one `task_suite_id`+version with
  matched model/tool/budget config; any accuracy delta below baseline
  forces `regressed` regardless of efficiency gain.
- **Anti-N=1 thresholds** (initial, tunable, recorded in the policy schema
  — not the prompt): a pattern may reach `candidate` at ≥3 observations
  across ≥2 distinct run lineages; `proposed` requires ≥2 distinct graph
  attempts or task suites. Single-run findings stay `observed` forever and
  are visible but never actionable.
- **Anti-thrash.** One open proposal per `target_surface` path; a cooldown
  (e.g. ≥ N runs or 14 days) before any surface can be re-proposed;
  supersession chain with immutable-after-acceptance records
  (`docs/decisions/README.md` rule); a hard cap on open proposals;
  mandatory `rollback` text. A `regressed` outcome bars re-proposing the
  same pattern without new evidence.
- **Secrets/redaction.** Observations carry normalized signatures and hash
  refs, never raw transcripts or environment dumps. Committed pattern
  records are the sanitized projection.

## 6. Cold start (`logs/runs/` is empty today)

1. **Backfill the corpus that already exists in prose.** Hand-transcribe
   `contract-burden-reduction.md` items 1–11 (+ the FR-20 audit table, the
   postmortems, `recovery-machinery-inventory.md`) into
   `blocker-pattern/1` records with `status: addressed` and their landing
   commits. This becomes the **gold set**: when journals exist, the miner
   is validated by asking whether it independently rediscovers items 2, 5,
   7, and 8 from raw events — a real acceptance test, not a plausibility
   argument.
2. **Collect-only mode until support exists.** Ship the miner with
   proposal drafting disabled by config; it emits observations and
   `observed` patterns only. Every run from now on is corpus.
3. **Volume target.** Calibrating against the known data (orbit: 2 graph
   attempts sufficed to qualify 6 items; FR-20: 101 attempts, 232 run
   dirs): enable proposal mode at roughly **20–30
   `production_lifecycle` FeatureRuns spanning ≥3 distinct programs**. At
   the README's $3–5/FeatureRun that is ~$100–150 of deliberate
   corpus-building.
4. **Blocker: no versioned task suite.** `NEXT_STEPS.md` required slice 8
   ("small versioned feature benchmark, accuracy and cost baseline before
   optimization") is **not done**. Without it, step 7's before/after is
   unsound — there is no `task_suite_id` to hold constant. Building that
   suite is a hard prerequisite for the monitoring milestone (not for
   mining), and is arguably the highest-leverage thing to do first.
5. **Corpus portability.** Decide early whether journals get exported
   (sanitized, hash-anchored) to a durable local store outside the
   checkout, since ephemeral sessions plus `.gitignore` will otherwise
   discard the very evidence this system needs.

## 7. MVP — three milestones, each independently useful

**M1 — Forensic miner + corpus (no proposals).**
`schemas/blocker-observation.schema.json`,
`schemas/blocker-pattern.schema.json`;
`observability/run_forensics.py` + `improvement_index.py`;
`scripts/self_improve.py mine|cluster`; backfilled hand corpus; unit tests
over `tests/fixtures/plan_graph_parallel/*` and fabricated journals
(classified `fabricated_fixture`, therefore excluded from aggregates —
which the tests should assert). *Value alone:* the FR-20 audit table
generated automatically instead of by hand, plus a machine-readable version
of the living diagnosis.

**M2 — Proposal + gated approval (human implements).**
`improvement-proposal` / `-subject` / `-approval-receipt` /
`-operator-approval` schemas; generalize
`plan_approval.prepare_approval`/`issue_receipt` over subject kinds;
`graphrun/improvement_program.py`; `self_improve.py propose|prepare|issue`;
CI validation of committed artifacts. Implementation still done by a
human-launched FeatureRun using the emitted decomposition. *Value alone:*
every harness change acquires an evidence-bound, gated, falsifiable
justification.

**M3 — Close the loop.**
`improvement-outcome/1`; `self_improve.py monitor` (before/after cohorts
keyed to the versioned task suite from §6.4); automatic pattern-status
transitions and supersession; auto-drafted `docs/decisions/` record on
`confirmed`; retirement checks in the `check_workaround_retirement.py`
style. *Value alone:* the first mechanism in the repo that can say a landed
harness change actually worked.

Deliberately deferred: fully autonomous dispatch of the implementing run
(M2/M3 keep a human between proposal and execution); cross-repository
corpora; any ML clustering beyond exact/normalized signature grouping.

## 8. Open questions

1. **Task-suite identity is the load-bearing gap.** No versioned benchmark
   exists (`NEXT_STEPS.md` slice 8 open). Build it before M3, or accept
   weaker "same program, same node" cohorts as the comparability unit?
2. **Deterministic vs. model-driven mining.** Extraction is scoped as
   deterministic; the model is reserved for the generalizability verdict
   and drafting. Should the model also propose *new* signatures (higher
   recall, much weaker reproducibility)?
3. **Corpus provenance is mixed.** The richest failure evidence (FR-20)
   came from Retinology's older harness fork, which lacks mechanisms this
   repo has. How should patterns mined from a divergent fork be weighted —
   or excluded?
4. **Relaxation vs. addition asymmetry.** Most landed CB items *removed*
   gates. Removals directly touch the accuracy gate and are the
   highest-risk class, yet the strongest evidence ("defeated by mechanical
   compliance") supports exactly those. Should relaxation proposals
   require a stricter bar than additions, or the same?
5. **Is `AGENTS.md` in scope at all?** An agent proposing edits to its own
   operating contract is the maximal blast radius. Options: forbid
   outright; allow with elevated attestation; or allow only via a
   `docs/decisions/` record that a human transcribes into `AGENTS.md`.
6. **Who is the operator?** The receipt requires a named attesting actor.
   Single-operator repo today — is a second reviewer needed for
   `gate_relaxation` proposals?
7. **Journal retention and cost.** Mining needs journals to persist;
   they're 0700 local and gitignored, and 232 dirs for one program is not
   small. What retention window, and is a sanitized export worth building
   in M1?
8. **Trigger ownership.** The run-completion wrapper is currently
   operator-owned launcher code. Should the trigger move into the shipped
   finalization path so it can't be forgotten — itself the kind of
   "prompt-owned transition" the harness contract objects to?
