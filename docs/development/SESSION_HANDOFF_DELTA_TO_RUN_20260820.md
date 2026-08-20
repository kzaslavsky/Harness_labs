# Session handoff: the delta-to-run pipeline (2026-08-20)

Self-contained brief for a fresh session. Goal: implement the missing
pieces of the pipeline that turns **statements about required or deviant
behavior** into an **approved, running PlanGraph**, on top of the machinery
that exists on main at `59c7fd9`.

## Ground rules

- Work only in a dedicated worktree branched from `main`; never the
  primary checkout:
  `git worktree add .claude/worktrees/<name> -b claude/<name> main`
- The base must stay pristine including untracked files (writable workers
  refuse otherwise). Log only under gitignored paths (`logs/runs/`,
  `logs/plan-approval/`).
- Consumers import by full module path; `harness_labs/__init__.py` is out
  of scope. Nothing under `harness_labs/core` may import
  `harness_labs.plangraph` (`tests/test_import_boundaries.py` enforces).
- No real-browser dependency may enter harness CI (stub driver +
  `UI_FIDELITY_PYTHON` resolution; skip with a recorded reason).
- Full suite gate: `python3 -m pytest tests/ -q` (968 passed / 2 skipped /
  1 xfailed at `59c7fd9`).

## The three delta sources, unified

All inputs reduce to one interface — evidence-backed statements of a delta
between observed and required state:

- **(a) An approved spec/PRD** (e.g. a UI mockup) is not itself a stream of
  statements; it is the **target**: pinned by digest + snapshot at
  `campaign_opened` (`harness_labs/plangraph/convergence_campaign.py`).
  Deviations *from* it arrive later via (b)/(c).
- **(b) Human statements** ("the review-node icon is inconsistent with the
  mockup") — arrive ad hoc, mid-round or between rounds.
- **(c) Delta-generator agents** — the UI-fidelity inspector
  (`harness_labs/core/ui_fidelity_inspector.py`) over capture evidence
  (`scripts/ui_fidelity_capture.py`), or any future measurer. The
  convergence outer loop's audit step IS this: (c) and "convergence
  analyzer" are the same functionality.

(b) and (c) meet the same contract: a **finding** — validated envelope
(`harness_labs/core/controller_results.py`) plus fidelity block
`{file, subject, required_paths, confidence, supersedes_key}`, keyed
`(file, subject)`, with `file ∈ required_paths`. Ingest
(`harness_labs/plangraph/convergence_ledger.py`) rejects findings missing
`file`/`subject`/`required_paths`. `required_paths` is the load-bearing
field: node ownership derives from it alone, and wrong attribution
recreates scope-fence review churn (the dominant cost of the CC campaign,
see `docs/decisions/0007-in-graph-escalation-bounded-unsealing.md`).

## The corrected pipeline

Numbering the operator's sketch, corrected in two places (canonicalization
is parsing, not a gate, and issue is identity-binding, not re-analysis):

```
0. target pinned            campaign_opened: digest + snapshot  [exists]
1. intake                   statements -> contract findings     [BUILD: FI]
2. plan synthesis           open findings -> decomposition JSON
                            WITH observable declarations        [BUILD: plan step]
   (canonicalize+validate runs mechanically here and on every
    subsequent edit — it is a parser, not an approval step)     [exists]
3. refine                   prepare→warn→revise loop: overlap
                            narrowing, serialization, S3/S9
                            proposals; iterate                  [exists]
4. prepare                  subject.json + gate-evidence.json:
                            warnings w/ digests + conformance
                            report (always emitted)             [exists]
5. approve                  human writes operator-approval.json
                            (warning_acknowledgements,
                            conformance_overrides). The only
                            human step.                         [exists]
6. issue                    receipt. NOT a second analysis:
                            re-derives from the exact bytes on
                            disk and refuses on any drift
                            between what was approved and what
                            is being issued (TOCTOU guard); the
                            enforcement point of the ack gate.  [exists]
7. register + run           admission validate -> registration
                            (digest, approval-bound retry
                            lineage, automatic_recovery) ->
                            PlanGraph run; budgets, autoresume,
                            recovery authority                  [exists]
```

Conformance arming (deliberate design, ADR-adjacent ruling 2026-08-19):
analysis + report always run for every plan; **blocking** arms only when
the plan declares S5 observables on its criteria or a caller passes
`enforce=` (`harness_labs/plangraph/decomposition_conformance.py`,
`plan_approval.py`). Armed plans retain per-criterion per-node overrides
with recorded reasons. Hand-authored/exploratory plans stay advisory-only.
Step 2 is what arms real campaigns: generated plans must stamp observables.

## What exists on main (59c7fd9) — do not rebuild

| Piece | Where |
|---|---|
| Finding contract + ledger + ingest + derived views | `harness_labs/plangraph/convergence_ledger.py`, `harness_labs/core/convergence_contract.py` |
| Campaign checkpoint, artifact store, target pin, config | `harness_labs/plangraph/convergence_campaign.py` |
| Measurer: capture CLI + inspector role | `scripts/ui_fidelity_capture.py`, `harness_labs/core/ui_fidelity_inspector.py` |
| Campaign driver (measure/ingest/rule/plan/approve/run/close/resume/state) | `scripts/run_convergence_campaign.py` |
| Canonicalize + validate | `harness_labs/plangraph/plan_graph_contract.py` (closed top-level key set — a new payload field REQUIRES editing this contract), `plan_graph.py:validate_plan_graph_plan` |
| Refinement loop + injected judge | `harness_labs/plangraph/plan_refinement.py`, `scripts/approve_plan.py refine` |
| Prepare/approve/issue + warning kinds + ack gate + conformance report | `harness_labs/plangraph/plan_approval.py` |
| Conformance analyzer (S1–S10, graded, overrides, proposals) | `harness_labs/plangraph/decomposition_conformance.py` |
| Register/run/resume/budget CLI | `scripts/run_plan_graph.py`, `plan_graph_budget.py`, `scripts/plan_graph_autoresume.py`, `scripts/plan_graph_recover.py` |
| Recovery authority incl. transfer_ownership; directory-grant finding routing; any-width frontier recovery; review scope guard; liveness lease | `plan_graph_authority.py`, `review_fix.py`, `harness_labs/core/controller_liveness.py` (landed 2026-08-19 in parallel — reconcile designs against these, they are labeled "campaign preconditions") |
| Proven campaign launcher (experiments-grade) | `experiments/run_convergence_plan_graph.py` |
| Lifecycle proof of the whole slice | `tests/test_convergence_lifecycle.py` |

Both follow-ups from the CC campaign are DONE and merged (2026-08-20):
CC-08 in-graph escalation with bounded unsealing (escalation judge seat,
`schemas/plan-graph-escalation-judgment.json`, escalation routed per
launch, ADR 0007 amended for the refusal channel — see merge `04002e8`)
and the red_green_check per-phase timeout fix (`706e555`). Pitfall 1 below
is therefore mitigated in-graph now, and pitfall 4's flake is fixed;
both remain listed for their design lessons.

## What to build (four lanes; FI ∥ SN, then MC, then LK)

**FI — finding intake.** `harness_labs/plangraph/finding_intake.py` +
`scripts/report_finding.py` + tests. Free-text statement + repo + target →
contract-valid finding(s): root-cause `required_paths` by searching the
code; derive `(file, subject)`; `confidence: S` unless capture evidence
attached; set `requires_disposition` for judgment calls. Must round-trip
the REAL ledger ingest validator until clean; ambiguity → ask the
operator, never guess. CLI seals findings as an operator-reported audit
artifact via the campaign artifact store and folds via the existing
idempotent ingest — this gives the dynamic mid-campaign flow (operator
messages a session; session appends a keyed finding for the next round)
with no new record types. Also batch mode for seed-audit transcription.

**SN — sanitizer media-type contract.** Generalize
`pre_journal_sanitizer` (campaign config) to a per-media-type policy:
text artifacts scanned; binary artifacts by declared policy (e.g.
fixtures-only receipts make screenshots trivially clean). Add a capture
`--dry-run` that runs the sanitizer over a sample bundle without
journaling. Mechanism is domain-neutral; the PHI policy content is
product config, not harness code.

**MC — measurer commissioning.** Pre-campaign calibration phase: run the
capture matrix N times against the real target; per-cell stability report;
tune stabilization until unstable rate clears a declared threshold;
chronically unstable cells surface for explicit ruling. Run inspector
recall calibration against transcribed seed findings in the same phase
(before round 1, not at first post-repair audit). Both reports are
artifacts a campaign-open checklist requires.

**LK — launcher kit + plan step.** Extract the proven launcher into
`harness_labs/graphrun/campaign_launcher.py` with a product config
surface, carrying ALL campaign-learned defaults: operator-notes folded
into implementer/fixer/**reviewer** instructions; anti-placeholder
hardening (the deliverable floor killed two attempts); recovery_limit=5 /
continuation_recovery_limit=3; 7200s coordinator silence tolerance;
dirty-baseline wiring; venv profile-builder hook. The plan-synthesis step
lives here too: emit decompositions with observable declarations per
criterion (arming conformance) and pass `enforce=` at approve for
generated plans.

## Pitfalls (each cost real attempts in the CC campaign)

1. Wrong `required_paths` → scope-fence churn: a reviewer finds a defect
   the fixer cannot legally touch; the loop burns cycles to a block.
2. Reviewers must see operator rulings, or every fresh reviewer
   re-litigates them.
3. Placeholder text in structured summaries hard-fails the deliverable
   floor — instruct workers explicitly.
4. Empty retry frontier is refused; a finalize-gate flake forces a full
   leaf-node re-run (re-IMPLEMENTATION, not re-review). See
   `docs/development/relax-gate-timeout-flake-20260819.md`.
5. `canonical_plan_graph_payload` rejects unknown top-level keys — never
   invent payload fields without editing the contract module.
6. Coordinator `timeout_seconds` is max SILENCE between stream events; it
   must exceed the longest worker runtime.
