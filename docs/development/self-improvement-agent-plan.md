# Self-Improvement Agent — Engineering Plan (rev 2)

Status: proposed, awaiting operator approval. Registerable decomposition:
[`self-improvement-decomposition.json`](self-improvement-decomposition.json).
Supersedes the rev-1 draft (branch
`claude/hipaa-retinology-inference-resale-fez076`, commit `b6e2e9b`),
which was written on a lineage that predated the convergence,
engineering-memory, and delta-to-run merges and therefore designed
parallel machinery this revision deletes in favor of reuse.

Section headings are stable citation anchors (`[SI-…]`): decomposition
runs cite sections by slug and workers read only the cited sections.

## SI-00 — Architecture: one braid, three existing systems [si-00-architecture]

The self-improvement agent is **not a new subsystem**. It is a scheduled
composition of three subsystems that already exist on this lineage:

1. **Engineering memory is the audit substrate.** The scheduled job
   mines local run journals (`logs/runs/*`) into blocker observations and
   patterns, joining `harness_labs/plangraph/finding_history.py`
   (cross-campaign ruled-key recall) and
   `harness_labs/core/decision_registry.py` (active ADRs governing the
   paths a proposal would touch). Journal authenticity comes free from
   `project_run_metrics()` (`harness_labs/observability/run_metrics.py`),
   which refuses any run whose audit hash chain fails verification.
2. **Delta-to-run is the planning pipeline.** An approved improvement
   proposal is expressed as a batch of finding-shaped specifications
   (`file`, `subject`, `required_paths`, assertion). Those enter the
   existing intake → synthesis → approval → launch pipeline
   (`finding_intake.py`, `plan_synthesis.py`, `scripts/approve_plan.py`,
   `harness_labs/graphrun/campaign_launcher.py`) exactly as product
   findings do. No improvement-specific approval machinery exists.
3. **Convergence is the outer loop.** The improvement campaign uses a
   real `ConvergenceLedger` under `logs/improvement/campaigns/<id>/`:
   each round synthesizes a PlanGraph from `open_findings()`, executes it
   with GraphRun machinery, then **re-audits** — re-runs the miner plus
   the specification assertions — and folds verdicts. Only
   `observed_fixed` (the blocker signature gone / the assertion passing
   on fresh evidence) closes a key. Rounds repeat until every
   specification key is closed or ruled `waive`, or the round bound is
   hit.

The lifecycle:

```
schedule ──▶ audit (mine + cluster; EM joins)          [deterministic]
        ──▶ propose (improvement-proposal/1 drafts)    [model, bounded]
        ──▶ OPERATOR accepts proposal                  [human ruling]
        ──▶ open improvement campaign
              (success_criteria → finding batch → ConvergenceLedger)
        ──▶ round: plan_synthesis → approve_plan (receipt) ──▶ PlanGraph
        ──▶ re-audit → verdicts → next round …         [until closed]
        ──▶ close: docs/decisions/ record → decision registry
```

Two human gates, both pre-existing in kind: the **proposal ruling**
(accept/reject/waive, recorded as a committed digest-bound record) and
the **per-round plan-approval receipt** (`scripts/approve_plan.py
prepare`/`issue` — the only door that dispatches work; this plan adds no
parameters to it and no new receipt schemas, unlike rev 1).

**Layer placement is boundary-driven** (`scripts/dev/check_import_boundaries.py`,
`ALLOWED`: `observability→core+self`, `graphrun→everything`):

- Mining and clustering (`run_forensics.py`, `improvement_index.py`) go
  in `harness_labs/observability/` — they read journals and import core
  only.
- Everything that joins plangraph machinery (`finding_history`,
  `ConvergenceLedger`, `plan_synthesis`) or dispatches runs lives in
  `harness_labs/graphrun/improvement_program.py`. The engineering-memory
  join therefore happens at the graphrun layer, not in observability —
  `finding_history` is a plangraph module that observability may not
  import. `decision_registry` is core and may be used at either layer.

**What is committed vs. local.** `logs/runs/*` and
`logs/improvement/**` stay gitignored (mining state, unaccepted drafts,
campaign ledgers). Committed artifacts live under `docs/improvement/`:
accepted proposals, the pattern records they cite (sanitized,
hash-referencing local journals), per-campaign decomposition JSONs, and
the closing decision record in `docs/decisions/`.

**Engineering-memory hard rules honored** (violations fail review):
no new `state-ledger` record kinds and no ledger writer changes; no new
`prepare_approval`/`issue_receipt` parameters; no new
`canonical_plan_graph_payload` top-level keys; no third-party
dependencies — artifact checkers are hand-written.

## SI-01 — Improvement artifact schemas and checker [si-01-schemas]

Objective: the three improvement artifact contracts plus a deterministic
checker, reusing existing enums verbatim — no new taxonomies.

New schemas (`protocol: {"const": "…/1"}`, `additionalProperties:
false`, in the style of `plan-approval-receipt.schema.json`):

- `schemas/blocker-observation.schema.json` — one blocker instance in
  one run: `run_id`, `run_kind`, `evidence_classification` (enum from
  `schemas/audit-event.schema.json`), `node_id|null`, `attempt_id`,
  `phase`, `classification` (enum from `schemas/block-escalation.json` /
  `schemas/retry-budget-ledger.json`), `rule_id|null` (stable ids from
  `classify_verification_failure()`), `signature` (normalized,
  secret-free failure key), `first_event_sequence`, `event_hashes[]`
  (chain anchors), `resolution` (`self_recovered | repair_attempt |
  retry_renewed | operator_intervention | prompt_workaround |
  transferred | unresolved_blocked`), `resolution_cost` (`retries`,
  `repair_dispatches`, `wall_clock_ms`, `tokens`, `diff_churn_lines`),
  `artifact_refs[]` (`{path, sha256}`), `redaction_applied`.
- `schemas/blocker-pattern.schema.json` — a cluster across runs:
  `pattern_id`, `signature`, `classification`, `status` (`observed |
  candidate | proposed | addressed | superseded | rejected`), `support`
  (`observation_count`, `distinct_run_count`, `distinct_lineage_count`,
  `distinct_task_suite_count`), `first_seen_at`/`last_seen_at`,
  `observations[]` refs, `cost_aggregate` (median + tail),
  `fixes_employed[]` (mechanism incl. `prompt_workaround` — the
  generalizability tell), `generalizability` (`verdict ∈ {one_off,
  policy_gap, gate_defeated_by_mechanical_compliance,
  superseded_by_existing_mechanism, environmental}`, `rubric_id:
  "burden-admission/1"`, `rationale`, `counterexamples[]`),
  `recurrence[]` — engineering-memory join results: prior ruled keys
  from `finding_history` and governing decision ids from the registry.
- `schemas/improvement-proposal.schema.json` — the actionable output:
  `proposal_id`, `pattern_ids[]`, the `decision.schema.json` reasoning
  vocabulary (`question/choice/alternatives/rationale/evidence/
  consequences/reversible/status`), the Complexity-admission triple
  (`demonstrated_failure`, `production_consumer`,
  `end_to_end_assertion`) — all three **required** so an unfalsifiable
  proposal cannot serialize — `target_surface[]` (`{path, kind ∈ {doc,
  schema, gate, policy, code}, governing_decisions[]}`),
  `accuracy_risk ∈ {none, gate_relaxation, gate_addition}`,
  `success_criteria[]` — **each entry finding-shaped**: `file`,
  `subject`, `required_paths[]` (`file ∈ required_paths`), `statement`,
  `assertion` (`{argv, timeout_seconds}` or a signature-absence check) —
  this is the bridge into delta-to-run intake — `rollback`, and
  `ruling|null` (`{disposition ∈ {accept, reject, waive}, actor,
  statement, ruled_at}`; never machine-authored).

`scripts/dev/check_improvement_artifacts.py`: hand-written checker (no
`jsonschema`) validating any tree of committed improvement artifacts —
schema shape, enum membership, cross-references (every cited pattern
exists, every accepted proposal has a human ruling, `file ∈
required_paths` on every success criterion). Exit nonzero on any
violation; this is the CI entry point for committed artifacts.

## SI-02 — Forensic miner: the audit capture step [si-02-miner]

Objective: deterministic, read-only extraction of
`blocker-observation/1` records from local run journals.

`harness_labs/observability/run_forensics.py`:

- Enumerates `logs/runs/<run-id>/` roots; admits a run only when
  `project_run_metrics()` verifies its hash chain — authenticated input
  or nothing.
- Reads `events.jsonl` (retries, `status ∈ {failed, blocked}`,
  blocked/failed/recovering phases), `decisions.jsonl`,
  `checkpoint.json`, `summary.json`, `manifest.json`, and artifacts:
  review-ledger findings with `reopened_count > 0` / `fix_attempts` /
  `transferred`, retry-budget-ledger `abandoned`/`blocked`/`extended`/
  `gate_changed` events and their `failure_keys`, block-escalation
  records, workspace-change receipts.
- **Hard filter: only `evidence_classification: production_lifecycle`
  aggregates.** Synthetic and `fabricated_fixture` runs are parsed but
  excluded from every aggregate; tests assert the exclusion.
- Normalizes signatures (ids/paths/timestamps stripped; prefer stable
  `rule_id`s and `failing_identifiers` sets over message text). No raw
  transcripts, no environment dumps — hash refs only.
- Idempotent and watermarked by run id under `logs/improvement/state/`;
  re-running mines only new run directories. Pure library + thin CLI
  wiring later in SI-05.

## SI-03 — Pattern index and thresholds [si-03-index]

Objective: deterministic clustering of observations into
`blocker-pattern/1` records with anti-N=1 thresholds; model judgment is
confined to the generalizability verdict.

`harness_labs/observability/improvement_index.py`:

- Groups by exact/normalized `signature` + `classification`; no ML
  similarity, by design.
- Thresholds (in code and recorded in each pattern record, not in a
  prompt): `candidate` at ≥3 observations across ≥2 distinct run
  lineages; proposable at ≥2 distinct graph attempts or task suites.
  Single-run findings stay `observed` forever — visible, never
  actionable.
- Computes `support` and `cost_aggregate` (median + tail) per the
  logging contract's aggregation discipline.
- Emits pattern records to `logs/improvement/patterns/`; the
  generalizability `verdict` field starts `null` and is filled by the
  bounded model step in SI-04 (input: the pattern record set, never raw
  transcripts).
- Anti-thrash state lives here: one open proposal per `target_surface`
  path, cooldown (≥14 days or ≥N new runs) before a surface can be
  re-proposed, hard cap on open proposals, and a `rejected`/`regressed`
  history bar against re-proposing the same pattern without new
  observations.

## SI-04 — Improvement program: EM joins, proposals, rulings [si-04-program]

Objective: the graphrun-layer composition that turns patterns into
operator-facing proposals.

`harness_labs/graphrun/improvement_program.py`:

- **Engineering-memory join** (legal only at this layer):
  `finding_history.fold_campaigns` over known campaign journals
  annotates each pattern with prior ruled keys touching the same paths
  (`for_paths`) — a pattern a previous campaign already ruled arrives
  with that lineage attached; `decision_registry.load_decisions`
  populates `governing_decisions` on every `target_surface` entry, and a
  proposal touching paths governed by an active ADR must cite it or the
  drafter refuses. Registry `Inconsistency` records are surfaced in the
  proposal packet, never resolved silently.
- **Drafting** (model, bounded): one `improvement-proposal/1` per
  qualifying pattern; refuses to emit unless the Complexity-admission
  triple and at least one executable `success_criteria` assertion can be
  filled. Proposals targeting `AGENTS.md`,
  `docs/architecture/harness-contract.md`, or any accuracy gate carry
  `accuracy_risk: gate_relaxation` and require the ruling statement to
  name the gate and the superseding mechanism (`burden-admission/1`
  clause 2, mechanized).
- **Ruling packet**: pattern evidence, cost aggregate, recurrence
  lineage, governing decisions, candidate dispositions. The operator's
  ruling is written into the proposal record and the record is committed
  — acceptance is a digest-bound, reviewable artifact. Rulings are never
  machine-authored.
- Unit tests exercise the joins against fixture journals and a fixture
  decision tree; no live model in tests (drafter takes an injectable
  judgment callable, mirroring the inspector-injection pattern in
  convergence tests).

## SI-05 — Convergence bridge, loop driver, CLI [si-05-loop]

Objective: an accepted proposal becomes a bounded convergence campaign
over the harness repo, dispatching successive PlanGraphs until all
specifications are met.

`harness_labs/graphrun/improvement_loop.py` (the campaign orchestration
library, including close-out promotion) plus `scripts/self_improve.py`
(thin argparse over it; JSON to stdout, errors to stderr, exit nonzero),
subcommands:

- `audit` — run SI-02 mining + SI-03 clustering. Deterministic; safe on
  a schedule. `--propose-if-ready` additionally drafts proposals for
  patterns past threshold (SI-04).
- `open --proposal <path>` — for an **accepted** proposal: create
  `logs/improvement/campaigns/<campaign-id>/`, open a real
  `ConvergenceLedger`, and ingest the proposal's `success_criteria[]` as
  the seed finding batch via the finding-intake path (`file`, `subject`,
  `required_paths` are already intake-shaped; `confidence: "C+S"` —
  observed in journals, root-caused in the proposal). Sealing follows
  the intake rule: seed artifacts seal, they are not folded mid-round.
- `round` — synthesize the next PlanGraph from
  `ConvergenceLedger.open_findings()` via `plan_synthesis` (one run per
  `required_paths` group, `OBSERVABLE:` trailers armed), write the
  decomposition under the campaign root, then stop and print the
  `approve_plan prepare`/`issue` commands. **The existing receipt is the
  only dispatch door**; after `issue`, `round --launch` starts the
  PlanGraph via `campaign_launcher.build_campaign_launch_config`
  defaults in an isolated worktree, with
  `scripts/dev/red_green_check.py` finding-tests plus
  `python3 -m pytest tests/ -q` and
  `python3 scripts/check_repository_contracts.py` as regression gates.
- `remeasure` — after a round's candidate integrates: re-run the miner
  over post-round runs, execute each open key's `assertion`, and fold an
  audit result into the ledger. Only `observed_fixed` closes a key;
  unexecuted assertions fold as `unobserved` and block success
  termination (convergence verdict semantics, unchanged).
- `status` — campaign state: open/closed keys, round count, bound.

Termination: success when every specification key is `observed_fixed`
or ruled `waive`; failure when the round bound (default 4) is hit with
keys open — the campaign closes `incomplete` and the pattern reverts to
`candidate` with the attempt recorded. A post-close recurrence of the
signature (seen by any later `audit`) flips the pattern back from
`addressed` and bars re-proposal without new evidence.

Integration test: a fixture campaign over a toy repo journal corpus
runs open → round (stub driver) → remeasure → close without a live
model, in the style of `tests/test_convergence_campaign.py`.

## SI-06 — Scheduling, close-out, CI, docs [si-06-schedule-close]

Objective: recurrence that cannot be forgotten, a close that feeds
engineering memory, and validation of everything committed.

- **Schedule.** The durable contract is the repo-owned script:
  `scripts/self_improve.py audit --propose-if-ready`. Ship a launchd
  template (`docs/operations/self-improve.launchd.plist.example`) for
  the operator's machine — **CI cannot mine**: `logs/runs/*` is
  gitignored, mode-0700, local; a scheduled GitHub Action sees an empty
  corpus. A Claude Code routine may additionally wake a session to
  review drafted proposals, but it is a convenience trigger only — no
  required contract exists only in a prompt.
- **CI's real job**: run `check_improvement_artifacts.py` (built in
  SI-01 with all committed-tree assertions: every accepted proposal has
  a human ruling; every `addressed` pattern names its campaign and
  landing commit) over `docs/improvement/`, alongside the existing
  repository-contract and import-boundary checks.
- **Close-out.** A campaign that terminates successful promotes to
  `docs/decisions/NNNN-*.md` via `TEMPLATE.md` — "Validation and
  reversal" holds the before/after evidence; `Concerns-paths:` is filled
  from the proposal's `target_surface`, so the decision registry serves
  this ADR back as an `active-decision-notice` in every future plan
  approval touching those paths. The pattern flips to `addressed`. Where
  the improvement retired a workaround, a per-item check in the
  `check_workaround_retirement.py` style proves it stays gone.
- **Docs**: `docs/development/self-improvement-agent-guide.md` (agent
  how-to in the style of the delta-to-run and engineering-memory
  guides), registration in `docs/development/INDEX.md`, and a
  `.gitignore` entry for `logs/improvement/`.

## Guardrails (cross-cutting) [si-guardrails]

- **Read-only miner**: the audit stage writes only under
  `logs/improvement/`; capability-broker grants exclude `docs/`,
  `schemas/`, `AGENTS.md`, and code. Implementation happens only inside
  a receipted PlanGraph node with its own scoped grants.
- **Accuracy is a constraint, never a trade**: any accuracy regression
  in a round's gates forces the round to fail regardless of efficiency
  gain; `gate_relaxation` proposals additionally need the elevated
  ruling statement (SI-04).
- **Secrets/redaction**: observations carry normalized signatures and
  hash refs, never raw transcripts; committed pattern records are the
  sanitized projection.
- **Cold start**: backfill `contract-burden-reduction.md` items 1–11
  (plus the FR-20 audit and postmortems) as `status: addressed`
  patterns — the gold set. Acceptance test for the miner: it
  independently rediscovers items 2, 5, 7, and 8 from raw events once
  journals exist. Proposal mode stays config-disabled until ~20–30
  `production_lifecycle` FeatureRuns spanning ≥3 programs.

## Deferred and open [si-open]

Deferred by design: autonomous dispatch (a human holds both gates);
cross-repository corpora; ML clustering; longitudinal before/after
cohort comparison on a versioned task suite (`NEXT_STEPS.md` slice 8 is
still open — until it exists, "specifications met" means the campaign's
own assertion set, not a benchmark delta).

Open questions carried from rev 1, narrowed: (1) is `AGENTS.md` in
scope at all, or only via a human-transcribed decision record; (2)
should `gate_relaxation` require a second reviewer in a single-operator
repo; (3) journal retention window for the corpus, and whether a
sanitized export store is worth building early; (4) how to weight
patterns mined from the divergent Retinology fork.
