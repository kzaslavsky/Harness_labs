# Convergence Campaign Plan

Status: proposed; decomposition-reviewed (verdict:
DECOMPOSABLE-WITH-EDITS, edits applied). First application:
[`flow-editor-convergence-application.md`](flow-editor-convergence-application.md).
Registerable decomposition:
[`convergence-campaign-decomposition.json`](convergence-campaign-decomposition.json).
Supporting analyses: [`plangraph-node-sizing-review.md`](plangraph-node-sizing-review.md),
[`convergence-generalization-and-tooling-survey.md`](convergence-generalization-and-tooling-survey.md).

Section headings below are stable citation anchors: a decomposition run
cites sections by the slugs shown in brackets, and its workers read only
the cited sections. Citation discipline is a reading contract — the
harness validates key references, not text inclusion; the driver adds
the byte-identity check (AC-CC04-8).

## Purpose

A bounded outer loop that drives a codebase toward a declared target by
repeating: **measure** the delta between the current integrated candidate
and the target; **plan** a small PlanGraph from the concrete finding set;
**execute** it with existing GraphRun machinery; **re-measure**. Repair
nodes are never declared ahead of findings — each round's decomposition
is a function of an already-collected, evidence-backed delta set, so
ownership is decided at planning time with the findings visible.

The loop is a measurement loop, not a bookkeeping loop: a finding is
fixed only when a later audit observed it fixed.

## Definitions

- **Campaign** — one bounded pursuit of one target on one base lineage.
- **Round** — one plan→execute cycle. **Audit** — one measure pass.
  Audits follow every round automatically and never consume the round
  bound.
- **Target** — a pinned, digest-addressed statement of intent (design
  file, spec, policy) plus an amendment protocol.
- **Finding** — a keyed, evidence-backed delta between candidate and
  target.
- **Measurer** — a per-domain pair: deterministic *capture* (evidence
  acquisition with honest coverage) and judgment *inspection* (keyed
  findings over that evidence).

## Contracts

### Target [contracts-target]

`target: {kind, digest, snapshot_path}` pinned at `campaign_opened`; the
target file is snapshotted into the campaign root. Amendment requires a
`target_amended` record naming the new digest and the invalidation scope
(which keys and confirmed-good entries it voids); rulings carry forward.
A `target_amended` record without a stated invalidation scope sets the
derived blocked state. The target path is rejected as a repair-node
grant.

### Finding [contracts-finding]

The semantic envelope (`harness_labs/core/controller_results.py`:
validated `id`, `statement`, `category`, `severity`,
`requires_disposition`, `evidence_refs`, `source_finding_ids`) plus a
`fidelity` block carried **on each `findings[]` entry** (finding entries
accept extra keys; the top-level `details` object carries audit-level
fields such as sweep counts and coverage):

```json
{
  "file": "<anchor path>",
  "subject": "<short stable slug>",
  "required_paths": ["<paths the repair must touch>"],
  "confidence": "C | S | C+S",
  "supersedes_key": null
}
```

Invariant: `file ∈ required_paths`; ownership derives from
`required_paths` alone. A delta that cannot name repair paths cannot own
a repair node. Ledger key: `(file, subject)`. Ingest rejects findings
missing `file`, `subject`, or `required_paths`; an exact key match
against a fixed key raises a re-emission warning at the rule step.
`confidence`: `C` confirmed by interaction/measurement, `S` inferred
from source, `C+S` observed then root-caused.

### Verdicts [contracts-verdicts]

Every audit returns, for every prior `open` or `fix_claimed` key, exactly
one of:

`observed_fixed` (citing the capture cell and assertion evaluated) |
`reopened` | `unobserved` | `invalidated`

A key the inspector does not mention is `unobserved`. Only
`observed_fixed` closes a key. `unobserved` blocks success termination.
A verdict citing a capture cell recorded `unstable` cannot write
`finding_fixed`; the key remains `fix_claimed` until re-observed on a
stable cell. The inspector output validator rejects a result missing a
verdict for any prior key supplied in the task context.

### Rulings [contracts-rulings]

Findings with `requires_disposition: true` go to a human at the rule
step, each as a packet: target excerpt, quoted criterion, capture
citation, candidate dispositions, and prerequisite (`desk` — answerable
from documents — or `live` — needs the running system). Three
dispositions, closed set:

- `waive` — enters the inspector's exclusion set.
- `require_repair` — key stays open; ruling text becomes its acceptance
  statement.
- `amend_criterion` — one atomic transaction: planning commit updates
  the canonical criteria and digest; dependent findings are invalidated;
  superseded tests become a repair node's scope; the next approval binds
  the new digest.

A ruling contradicting a criterion it does not name is refused. Rulings
are never machine-authored.

## Campaign state

Location: `<run_root>/.convergence-campaigns/<campaign_id>/` (dot-dir;
skipped by the dashboard run catalog by design).

### Ledger [state-ledger]

Append-only JSONL, flock+fsync (the `JoinConflictResolutionStore`
discipline). Records:

- `campaign_opened` — domain; `target` per the target contract; base
  commit and its merge-base with the product default branch; predecessor
  graph id; seed-audit digest; repo-identity branch constraint; and the
  campaign config: `pre_journal_sanitizer` hook, inspector recall
  threshold, amendment-ratio threshold.
- `finding_opened`, `finding_fix_claimed` (projected from graph success;
  never terminal), `finding_fixed` (only from an `observed_fixed`
  verdict), `finding_reopened` (with `reason`; `reason: "base_rebase"`
  is stall-exempt and demotes every `finding_fixed` key to
  `fix_claimed`), `finding_ruled`, `confirmed_good` (admitted to the
  exclusion set only with a machine-checkable assertion; otherwise
  recorded as `watch` — still swept, findings there route normally
  through the open set), `target_amended`, `capture_coverage`.

Derived views: open set; exclusion set; stall state; coverage state;
**amendment ratio** (keys closed via `amend_criterion` / keys closed) —
printed at every termination; success above the declared threshold
requires explicit human acknowledgment.

Per-round state inside a running graph stays exclusively in the existing
review-ledger machinery; the campaign ledger carries only cross-round
facts, with round outcomes projected from review-ledger artifacts by one
adapter function.

### Checkpoint and artifact store [state-checkpoint-store]

- **Checkpoint** — `convergence-campaign-checkpoint/1`: atomic replace
  (write-temp + rename + directory fsync), monotonic sequence number,
  lifecycle field, owner/liveness stamp, and staleness rejection (names
  the base commit it believes current; a mismatch on load refuses).
- **Artifact store** — content-addressed (`artifacts/<digest>`, with
  size, media type, retention): every sanitized capture artifact the
  ledger references is atomically copied in at seal time. Worktree
  copies are working files; the store is the record.

## Driver

### Steps [driver-steps]

`scripts/run_convergence_campaign.py` — a sequencer that delegates node
launching, approval, and resume to existing machinery. Steps per round:

1. `measure(campaign_state) -> audit_result digest` — one named function
   boundary; capture runs as a controller preflight
   (`require_preflight_success=True`), inspection cites the receipt.
   Output sealed as one immutable content-addressed artifact.
2. `ingest(digest)` — folds exactly one sealed artifact; idempotent by
   digest.
3. `rule` — human dispositions; blocks until answered.
4. `plan` — the operator, agent-assisted (no planner code), emits a
   registration whose repair nodes' `allowed_paths` equal the union of
   their owned findings' `required_paths`, disjoint per the sizing
   criteria. No standing catch-all node: a mid-run surprise on an
   unowned path blocks fail-closed and routes to the per-node
   operator-relief path or the next round's seed — authority follows an
   observed finding.
5. `approve` — the driver first runs the admission refinement loop
   (`approve_plan.py refine`, `plan_refinement.refine_decomposition` on
   main — narrow-grant and serialize repairs with a diffable report),
   then renders the findings→owners→paths table plus every warning from
   `prepare` (main's `prepare` now emits `warnings`,
   `high_severity_warnings`, and `unclaimed_grants` directly, each with
   a `warning_sha256`; high-severity warnings hard-fail `issue_receipt`
   unless the operator approval carries a matching
   `warning_acknowledgements` entry with a reason — the
   discarded-warnings failure mode is closed on main). The table is
   committed in the product repo and listed in `referenced_artifacts`.
   The driver refuses to proceed
   while any warning is unacknowledged, refuses to render a packet when
   a run's criteria text is not byte-identical to the
   `acceptance_criteria` entry it names or a node's objective is absent
   from its cited sections at base, and never authors
   `operator-approval.json` — it halts for the human-written file.
   Machine approval, if ever enabled, requires its own
   `machine-attested-plan-approval/1` policy (approver identity +
   profile digest, allowed predicates, evidence snapshot, explicit human
   delegation, revocation, deterministic predicate recomputation) —
   deferred; see the deferral table. Other preconditions checked:
   pristine base worktree (untracked included); plan artifacts committed
   at HEAD; repo identity file present at base.
6. `run` — existing PlanGraph execution, with registration persisted,
   `--on-block-argv` set, an automatic-recovery authority baked in, and
   `resume --round N` deriving arguments from `escalation.json`.
7. `close` — the next audit launches automatically the moment the
   join + regression node seals. Base adoption: the round's joined
   candidate becomes the next base **only if the join + regression node
   sealed**; otherwise the next round re-bases on the current base,
   carries sealed node candidates via the existing reuse path, and
   harvests unrouted findings from child review-ledger artifacts (the
   graph result itself carries no findings, and on the common block
   path no candidate).

## Bounds and termination [bounds-termination]

- **3 repair rounds** per campaign (default). The post-repair audit is
  always permitted and never consumes the bound.
- **Stall** (escalates instead of launching another round): a key with
  two unsuccessful repair claims, or a detected fixed/reopened cycle. A
  key merely open across two audits without an intervening repair is
  aging, surfaced at the rule step. A new finding whose `file`
  intersects a prior round's repair grants is tagged
  `regression_suspect` — it orders the rule step, never stalls alone.
- **Success** requires, on the final audit: zero new required findings;
  every key `observed_fixed` or ruled (no `unobserved`); full required
  capture coverage (from `capture_coverage`; unreachable required cells
  block); inspector recall at the configured calibration threshold;
  amendment ratio acknowledged if above the configured threshold; and —
  the first time a campaign reaches this state — a second independent
  dry inspector session.
- **Blocked** end states (ledger + pinned base + checkpoint are the
  handoff): rulings unanswered; stall; round bound exhausted with keys
  open; target amended without scope; sanitizer failure; predecessor
  graph still resumable at open; findings the planner cannot decompose
  into disjoint owners.
- Exhaustion never waives findings.

## Measurer requirements [measurer-requirements]

Capture must: acquire evidence deterministically (readiness gating; an
end-state double-read whose digest disagreement marks a cell
`unstable`); report per-cell status honestly (`ok`/`unreachable`/
`unstable`); exit zero whenever it ran (statuses recorded), nonzero only
when it could not run at all; and pass every artifact through the
campaign's `pre_journal_sanitizer` hook before anything is journaled,
digested, or injected into worker context. The capture script resolves
its browser interpreter from `--python` (default `sys.executable`);
whether a real browser or a stub driver executed is recorded in the
receipt, never inferred. An `unstable` cell makes measurement
inconclusive: findings keep their severity, but the key cannot reach
`observed_fixed` until re-observed on a stable cell.

Inspection must: read the target as source where the target is
inspectable; sweep with a bounded lens set plus at most one confirming
sweep (a non-dry final sweep is itself a finding); tag confidence; emit
findings per the contract; and return the per-key verdicts.

The domain filter for future measurers: the delta must localize to file
paths, or it cannot participate in PlanGraph ownership.

## Node sizing criteria [sizing-s1-s10]

Empirical basis: `plangraph-node-sizing-review.md`. Conformance is on by
default for generated graphs; overrides are per-criterion, per-node,
with a recorded reason; no blanket bypass. Main has meanwhile grown the
scaffolding S1–S10 slot into: `_sibling_overlap_warnings` plus
`_unclaimed_grant_warnings` with typed warning-kind constants,
`warning_identity()` digests, a hard acknowledgment gate for
high-severity warnings in `issue_receipt`/admission, and a
prepare→warn→revise refinement loop
(`harness_labs/plangraph/plan_refinement.py`, `approve_plan.py refine`)
with narrow-grant and serialize repairs. S1-at-directory-granularity and
S4–S9 remain new work; the conformance analyzer registers its findings
as new warning kinds beside the existing constants and rides the
existing acknowledgment gate rather than inventing an enforcement
channel.

- **S1** No two dependency-unordered nodes share a writable path, at
  file or directory granularity. *Block.*
- **S2** Every grant is an explicit file (or declared create); no
  directory grants. *Block.*
- **S3** Shared writers are serialized by `depends_on`, never by prose
  discipline. *Auto-fix as a proposal the operator re-commits — the
  analyzer never mutates an approved decomposition in place.*
- **S4** A node's grants equal the union of its owned findings'
  `required_paths`; no grants on paths its objective disclaims.
  *Block.*
- **S5** Every criterion carries a machine-readable observable
  declaration — `{kind: file|test_id|selector|command, referent}` — and
  never delegates pass/fail to an external document. *Block when the
  declaration is absent.*
- **S6** Every criterion's observable referent lies within the node's
  grants and is reachable from the node's `verification_argv`. *Block.*
- **S7** Exit checks are satisfiable within the node's own grants
  (including inherited-region merge obligations). *Block.*
- **S8** The verification gate is no larger than the criteria set;
  repo-wide invariants it pins are criteria or move downstream. *Warn,
  acknowledgment required.*
- **S9** Fan-in ≤ 3 for repair nodes; only the join/regression node
  exceeds it, and it carries integration criteria only. *Auto-fix
  (propose intermediate join) as a proposal, report.*
- **S10** ≤ ~8 repair nodes per round; split otherwise. *Warn.*

Deliberately not criteria (measured negatives): no cap on criteria
count; no functional/visual split; no separate one-surface rule. The
check never auto-splits nodes — it blocks with a suggested split.

## Build order

Measurement before orchestration (harness-contract execution-first).
Scope note for every node: consumers import by full module path;
`harness_labs/__init__.py` is deliberately out of scope.

**Base note:** this branch merges cleanly into current main
(`00b4e79`), but main rewrote `plan_approval.py` (+194 lines: warning
kinds, acknowledgment gate, refinement loop) and added
`plan_graph_autoresume.py` and `verification_images.py`. **The CC graph
must be registered against a base that includes main** — CC-04, CC-03,
and CC-07 all delegate to machinery that exists only there. Merge main
into this branch (or rebase) before CC-00 completes.

### CC-00 Base establishment [build-order-cc-00]

A separately approved integration plan producing the campaign's base
candidate, with its own acceptance criteria; the predecessor graph
settled. Precedes all estimates.

### CC-01 Ledger core and finding contract [build-order-cc-01]

Implement the convergence campaign ledger: append-only flock+fsync JSONL
journal with the campaign record types, ingest-time finding-contract
validation, per-key verdict semantics, and the derived views (open set,
exclusion set, stall state, coverage state, amendment ratio). Also
creates `harness_labs/core/convergence_contract.py` holding the closed
verdict and disposition vocabularies, so `core`-layer consumers never
import from the `plangraph` layer. Files:
`harness_labs/plangraph/convergence_ledger.py`,
`harness_labs/core/convergence_contract.py`,
`tests/test_convergence_ledger.py`. ~1.5–2 d.

### CC-02 Checkpoint and artifact store [build-order-cc-02]

Implement the campaign checkpoint and content-addressed artifact store
per the state contract, including the campaign config surface
(sanitizer hook, recall and amendment-ratio thresholds) and the target
pin/amendment records. Files:
`harness_labs/plangraph/convergence_campaign.py`,
`tests/test_convergence_campaign.py`. ~1 d. Depends on CC-01.

### CC-03 First measurer [build-order-cc-03]

Domain capture script + inspector role + smoke test on a static fixture
(see the application doc for the UI instantiation). The fixture is a
directory grant (`tests/fixtures/convergence_fixture_app`) — a recorded,
deliberate deviation from S2, which nothing enforces until CC-07 builds
the enforcer. The smoke test resolves its interpreter from
`UI_FIDELITY_PYTHON`, exercises the receipt and exit contract through a
stub browser driver when no real browser is available, and records a
skip reason rather than passing silently. Evidence persistence reuses
main's `harness_labs/core/verification_images.py` (selection, size/count
budgets, atomic copy into the evidence catalog, `--add-dir` worker
grants) — CC-03 adds only the matrix walk and receipt on top. ~2–2.5 d.
Depends on CC-01 (imports the verdict vocabulary from
`harness_labs/core/convergence_contract.py`).

### CC-04 Driver [build-order-cc-04]

The measure/ingest/rule/plan/approve/run/close step machine per the
driver contract, delegating node launching, approval, and resume to
existing machinery — specifically, per-round attempt execution
(quiescence detection, frontier reconciliation, no-progress bounding,
bounded relaunch) is **delegated to main's
`scripts/plan_graph_autoresume.py`**, not reimplemented; the driver owns
only campaign-round sequencing and the campaign ledger/checkpoint
interactions. Files: `scripts/run_convergence_campaign.py`,
`tests/test_convergence_campaign_driver.py`. ~1.5 d after the
delegation. Depends on CC-02.

### CC-05 Lifecycle proof [build-order-cc-05]

One subprocess-level run of the full slice (capture → inspection →
findings → one approved repair graph → join → post-repair verdicts)
through shipped CLIs on the static fixture, driving
`scripts/run_plan_graph.py` with a deterministic scripted launcher
fixture. The test authors, inside its temporary repository, exactly the
two human inputs the flow requires — the `operator-approval.json` and
the scripted rule dispositions — and nothing else on the human's
behalf. Files: `tests/test_convergence_lifecycle.py`,
`tests/fixtures/convergence_lifecycle_launcher.py`. Gate for everything
after. Depends on CC-03 and CC-04.

### CC-06 Round 1 on the real product [build-order-cc-06]

Human rulings, human approval. A campaign action, not a node in the
harness build graph.

### CC-07 Conformance analyzer [build-order-cc-07]

The S1–S10 admission analyzer with graded enforcement, proposal-only
auto-fixes, per-criterion per-node override records, and the conformance
report emitted into `gate-evidence.json` (shape-checked by
`_validate_gate_evidence`, hash-bound through the receipt's
`gate_evidence` reference). Authored against main's rewritten
`plan_approval.py`: conformance findings register as new warning-kind
constants beside `SIBLING_OVERLAP_WARNING`/`UNCLAIMED_GRANT_WARNING`,
blocking rules ride the existing high-severity acknowledgment gate, and
S3/S9 proposals plug into the `plan_refinement.py` repair loop. Its verification also runs the neighboring
integration suites (`tests/test_plan_approval.py`,
`tests/test_plan_graph.py`, `tests/test_import_boundaries.py`) so
integration breakage lands inside a repairable node rather than only at
the finalize gate. Files:
`harness_labs/plangraph/decomposition_conformance.py`,
`harness_labs/plangraph/plan_approval.py` (modify),
`tests/test_decomposition_conformance.py`,
`tests/test_plan_approval.py` (modify). Depends on CC-05.

Dependencies, each a data dependency: CC-02 embeds CC-01's record types;
CC-03 imports CC-01's verdict vocabulary; CC-04 reads and writes
CC-01/CC-02 state; CC-05 executes CC-03's measurer and CC-04's driver
together; CC-07 consumes CC-05's proven registration shape. CC-03 and
CC-02/CC-04 share no files, so those lanes run parallel (S1). The
registerable decomposition is
[`convergence-campaign-decomposition.json`](convergence-campaign-decomposition.json);
CC-00 and CC-06 are not nodes in it.

## Deferred, with triggers

| Deferred | Trigger |
|---|---|
| Details JSON Schema registry | Ingest validation proved insufficient: a well-formed-but-wrong details object misrouted a repair |
| Image-typed evidence artifacts | A finding contested at the rule step whose deciding evidence is a screenshot |
| Capability broker wiring | An inspector claimed visual evidence with no capture receipt |
| Theme/target-interpretation contract | Two repair nodes shipped incompatible interpretations of one target rule |
| Walk-plan protocol schemas | One round needs two different capture matrices in one graph |
| Dashboard projection of campaign state | An operator needed the live round and had no answer but `cat` |
| Join-conflict operator tooling | First real sibling join conflict in a round |
| Second independent dry inspector session | A final audit actually reaches zero-new-findings with full coverage |
| Full double-execution of gesture scripts | Contested finding caused by nondeterminism the end-state re-read missed |
| Agent-independent convergence scalar | Inspector dryness and deterministic triage disagree at a termination |
| Cross-round pixel triage | First measurer works end to end |
| Crash re-entry hardening beyond checkpoint basics | First real mid-campaign crash |
| Machine (LLM) approval under `machine-attested-plan-approval/1` | One full convergence cycle completed with human approvals |
| Second domain (recommended: migration completeness) | First campaign reaches a real termination |

## Tests

### Lifecycle proof [tests-lifecycle]

The subprocess-level lifecycle run (CC-05) is the acceptance test for
the whole first phase; the unit suites below support it, never
substitute for it.

### Ledger and state tests [tests-ledger]

`tests/test_convergence_ledger.py`: round-trip; ruling semantics (only
`waive` excludes; `amend_criterion` transaction invariants);
`fix_claimed` never terminal; verdict semantics (missing → `unobserved`,
blocks success; unstable-cell verdicts cannot close); `base_rebase`
demotion; repair-attempt stall (two failed claims / cycle; aging ≠
stall); watch admission; ingest validation; idempotent ingest; lock
safety. `tests/test_convergence_campaign.py`: checkpoint
atomicity/staleness; artifact seal-copy; target pin and amendment-scope
refusal; config surface.

### Driver tests [tests-driver]

`tests/test_convergence_campaign_driver.py`: termination predicate
(coverage, recall, amendment-ratio acknowledgment); harvest on both
block paths; base-adoption; round bound with the audit outside it;
stall escalation and `regression_suspect` ordering; resume from every
step; predecessor refusal; no silent approval; byte-identity approval
precondition; audit-on-seal.

### Measurer tests [tests-measurer]

`tests/test_ui_fidelity_capture.py` against the static fixture: matrix
walk with an interaction script, console/network capture, end-state
stability re-read, coverage statuses, verdict-completeness enforcement,
stub-vs-real-browser recording, and the exact exit contract (zero
whenever capture ran; nonzero only when it could not run).

In-graph routing is already pinned by
`tests/test_plan_graph.py:147,179,237`; cited, not duplicated. The
finalize gate runs the full suite (`pytest tests/ -q`).
