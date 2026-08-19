# Convergence Campaign Plan

Status: proposed. First application:
[`flow-editor-convergence-application.md`](flow-editor-convergence-application.md).
Supporting analyses: [`plangraph-node-sizing-review.md`](plangraph-node-sizing-review.md),
[`convergence-generalization-and-tooling-survey.md`](convergence-generalization-and-tooling-survey.md).

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

### Target

`target: {kind, digest, snapshot_path}` pinned at campaign open; the
target file is snapshotted into the campaign root. Amendment requires a
`target_amended` record naming the new digest and the invalidation scope
(which keys and confirmed-good entries it voids); rulings carry forward.
Amendment without a stated scope blocks the campaign. A repair node may
never write the target path.

### Finding

The semantic envelope (`harness_labs/core/controller_results.py`:
validated `id`, `statement`, `category`, `severity`,
`requires_disposition`, `evidence_refs`, `source_finding_ids`) plus a
`details.fidelity` block:

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

### Verdicts

Every audit returns, for every prior `open` or `fix_claimed` key, exactly
one of:

`observed_fixed` (citing the capture cell and assertion evaluated) |
`reopened` | `unobserved` | `invalidated`

A key the inspector does not mention is `unobserved`. Only
`observed_fixed` closes a key. `unobserved` blocks success termination.

### Rulings

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
invisible to the dashboard catalog by design).

- **Ledger** — append-only JSONL, flock+fsync (the
  `JoinConflictResolutionStore` discipline). Records: `campaign_opened`
  (domain, target, base commit + merge-base with the product default
  branch, predecessor graph id, seed-audit digest, repo-identity branch
  constraint), `finding_opened`, `finding_fix_claimed` (projected from
  graph success; never terminal), `finding_fixed` (only from an
  `observed_fixed` verdict), `finding_reopened` (with `reason`; a base
  rebase is `reason: "base_rebase"`, stall-exempt, and demotes every
  fixed key to `fix_claimed`), `finding_ruled`, `confirmed_good` (only
  with a machine-checkable assertion; otherwise recorded as `watch` —
  still swept, findings there route normally), `target_amended`,
  `capture_coverage`.
- **Checkpoint** — `convergence-campaign-checkpoint/1`: atomic replace,
  monotonic sequence, lifecycle field, owner/liveness stamp, and
  staleness rejection (names the base commit it believes current; a
  mismatch on load refuses).
- **Artifact store** — content-addressed
  (`artifacts/<digest>`, with size, media type, retention): every
  sanitized capture artifact the ledger references is atomically copied
  in at seal time. Worktree copies are working files; the store is the
  record.

Per-round state inside a running graph stays exclusively in the existing
review-ledger machinery; the campaign ledger carries only cross-round
facts, with round outcomes projected from review-ledger artifacts by one
adapter function.

Derived views: open set; exclusion set; stall state; coverage state;
**amendment ratio** (keys closed via `amend_criterion` / keys closed) —
printed at every termination; success above a declared threshold
requires explicit human acknowledgment.

## Driver

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
   criteria below. No standing catch-all node: a mid-run surprise on an
   unowned path blocks fail-closed and routes to the per-node
   operator-relief path or the next round's seed — authority follows an
   observed finding.
5. `approve` — the driver renders the findings→owners→paths table plus
   every `prepare_approval` warning, commits it in the product repo, and
   lists it in `referenced_artifacts` (hash-bound into the approval
   subject). The driver never authors `operator-approval.json`; it
   halts for the human-written file. Machine approval, if ever enabled,
   requires its own `machine-attested-plan-approval/1` policy (approver
   identity + profile digest, allowed predicates, evidence snapshot,
   explicit human delegation, revocation, deterministic predicate
   recomputation) — deferred; see the deferral table.
   Preconditions checked: pristine base worktree (untracked included);
   plan artifacts committed at HEAD; repo identity file present at
   base; verbatim acceptance-criteria statements; zero unacknowledged
   sibling-overlap warnings.
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

## Bounds and termination

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
  block); inspector recall at the calibration threshold; amendment
  ratio acknowledged if above threshold; and — the first time a
  campaign reaches this state — a second independent dry inspector
  session.
- **Blocked** end states (ledger + pinned base + checkpoint are the
  handoff): rulings unanswered; stall; round bound exhausted with keys
  open; target amended without scope; sanitizer failure; predecessor
  graph still resumable at open; findings the planner cannot decompose
  into disjoint owners.
- Exhaustion never waives findings.

## Measurer requirements (domain-generic)

Capture must: acquire evidence deterministically (readiness gating; an
end-state double-read whose digest disagreement marks a cell
`unstable`); report per-cell status honestly (`ok`/`unreachable`/
`unstable`); exit zero whenever it ran (statuses recorded), nonzero only
when it could not run at all; and pass every artifact through the
campaign's `pre_journal_sanitizer` hook before anything is journaled,
digested, or injected into worker context. An `unstable` cell makes
measurement inconclusive: findings keep their severity, but the key
cannot reach `observed_fixed` until re-observed on a stable cell.

Inspection must: read the target as source where the target is
inspectable; sweep with a bounded lens set plus at most one confirming
sweep (a non-dry final sweep is itself a finding); tag confidence; emit
findings per the contract; and return the per-key verdicts.

The domain filter for future measurers: the delta must localize to file
paths, or it cannot participate in PlanGraph ownership.

## Node sizing criteria (generated graphs)

Empirical basis: `plangraph-node-sizing-review.md`. Conformance is on by
default for generated graphs; overrides are per-criterion, per-node,
with a recorded reason; no blanket bypass. The existing
`_sibling_overlap_warnings` (`harness_labs/plangraph/plan_approval.py:458`)
covers only a same-file subset of S1 and its output is currently
discarded by `scripts/approve_plan.py`; S1–S10 as specified are a new
admission subsystem.

- **S1** No two dependency-unordered nodes share a writable path, at
  file or directory granularity. *Block.*
- **S2** Every grant is an explicit file (or declared create); no
  directory grants. *Block.*
- **S3** Shared writers are serialized by `depends_on`, never by prose
  discipline. *Auto-fix (insert edge), report.*
- **S4** A node's grants equal the union of its owned findings'
  `required_paths`; no grants on paths its objective disclaims.
  *Block.*
- **S5** Every criterion names its own observable (file, test id,
  selector, state, quantified check); none delegates pass/fail to an
  external document. *Block, quoting the criterion.*
- **S6** Every criterion is observable from the node's own execution
  environment, or moves to a node/preflight that can observe it.
  *Block.*
- **S7** Exit checks are satisfiable within the node's own grants
  (including inherited-region merge obligations). *Block.*
- **S8** The verification gate is no larger than the criteria set;
  repo-wide invariants it pins are criteria or move downstream. *Warn,
  acknowledgment required.*
- **S9** Fan-in ≤ 3 for repair nodes; only the join/regression node
  exceeds it, and it carries integration criteria only. *Auto-fix
  (propose intermediate join), report.*
- **S10** ≤ ~8 repair nodes per round; split otherwise. *Warn.*

Deliberately not criteria (measured negatives): no cap on criteria
count; no functional/visual split; no separate one-surface rule. The
check never auto-splits nodes — it blocks with a suggested split.

## Build order

Measurement before orchestration (harness-contract execution-first):

1. **CC-00 Base establishment** — a separately approved integration plan
   producing the campaign's base candidate, with its own acceptance
   criteria; the predecessor graph settled. Precedes all estimates.
2. **CC-01 Ledger core + finding contract** (~1.5 d) —
   `harness_labs/plangraph/convergence_ledger.py`,
   `tests/test_convergence_ledger.py`: records, ingest validation,
   verdict semantics, derived views.
3. **CC-02 Checkpoint + artifact store** (~1 d) —
   `harness_labs/plangraph/convergence_campaign.py`, tests: atomic
   replace, staleness rejection, seal-time copy.
4. **CC-03 First measurer** (~2.5–3 d) — domain capture script +
   inspector role + smoke test on a static fixture (see application
   doc).
5. **CC-04 Driver** (~2 d) — `scripts/run_convergence_campaign.py`,
   `tests/test_convergence_campaign_driver.py`: step machine, approval
   rendering and preconditions, harvest on both block paths, base
   adoption, bounds.
6. **CC-05 Lifecycle proof** — one subprocess-level run of the full
   slice (capture → inspection → findings → one approved repair graph →
   join → post-repair verdicts) through shipped CLIs on a static
   fixture. Gate for everything after.
7. **CC-06 Round 1 on the real product** — human rulings, human
   approval.
8. **CC-07 Conformance analyzer (S1–S10)** — separately specified
   admission feature with its own tests.

Dependencies: CC-01 → CC-02 → CC-04; CC-03 independent after CC-00;
CC-05 needs CC-01..04; CC-06 needs CC-05; CC-07 after CC-05. Each edge
is a data dependency: CC-02's checkpoint embeds CC-01's ledger records;
CC-04's driver reads and writes both; CC-05 executes CC-03's measurer
and CC-04's driver together; CC-07 consumes CC-05's proven registration
shape. CC-03 shares no files with CC-01/CC-02/CC-04, so it runs
parallel (S1).

The registerable decomposition — nodes CC-01..CC-05 and CC-07 with
objectives, acceptance-criteria ids, explicit `allowed_paths`,
`path_intents`, `depends_on`, and per-node `verification_argv`, in the
registration schema the dashboard campaign used — is
[`convergence-campaign-decomposition.json`](convergence-campaign-decomposition.json).
CC-00 is a separately approved product-side plan and CC-06 is a
campaign action (running round 1), so neither is a node in the harness
build graph.

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

- Subprocess lifecycle test (CC-05) — the acceptance test; unit suites
  support it, never substitute.
- `tests/test_convergence_ledger.py` — round-trip; ruling semantics;
  `fix_claimed` never terminal; verdict semantics (missing →
  `unobserved`, blocks success); repair-attempt stall; `base_rebase`
  demotion; watch admission; ingest validation; idempotent ingest;
  checkpoint atomicity/staleness; artifact seal-copy; lock safety.
- `tests/test_convergence_campaign_driver.py` — termination predicate;
  harvest on both block paths; base-adoption; round bound with audit
  outside it; stall escalation; resume from every step; predecessor
  refusal; no silent approval; audit-on-seal.
- Measurer smoke test — matrix walk, interaction script, logs capture,
  stability re-read, coverage statuses, exact exit contract.
- In-graph routing is already pinned by
  `tests/test_plan_graph.py:147,179,237`; cited, not duplicated.
