# Self-improvement agent: how-to

Status: active

Audience: AI agents operating or extending the self-improvement loop in
harness_labs. Full design: `self-improvement-agent-plan.md` (rev 2);
decomposition: `self-improvement-decomposition.json`. Read this instead of
re-deriving the flow.

## The braid, not a new subsystem

Three existing systems compose into one scheduled lifecycle (plan section
SI-00):

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

Engineering memory (`finding_history.py`, `decision_registry.py`) is the
audit substrate; delta-to-run (`finding_intake` → `plan_synthesis` →
`scripts/approve_plan.py` → `campaign_launcher.py`) is the planning
pipeline; convergence (`ConvergenceLedger`) is the outer loop. This plan
adds no parallel machinery for any of the three — see delta-to-run and
engineering-memory's own agent guides for those layers.

## 1. Schedule (`scripts/self_improve.py`)

The durable contract is one repo-owned command:

```
scripts/self_improve.py audit --propose-if-ready
```

CI cannot mine — `logs/runs/*` is gitignored, mode-0700, local, so a
scheduled GitHub Action would see an empty corpus. Recurrence is instead an
operator-owned local schedule: copy
`docs/operations/self-improve.launchd.plist.example` to
`~/Library/LaunchAgents/`, fill in the two `REPLACE_ME` paths, and
`launchctl load` it. A Claude Code routine may additionally wake a session
to review drafted proposals, but that is a convenience trigger only — no
required contract exists only in a prompt.

Other `self_improve.py` subcommands (`open`, `round`, `remeasure`,
`status`) drive one campaign at a time; see
`harness_labs/graphrun/improvement_loop.py`'s module docstring and
`tests/test_self_improve_loop.py` for the full open → round → remeasure →
close pipeline, including the receipt-gated dispatch door.

## 2. Committed artifacts vs. local state

- **Local, gitignored**: `logs/runs/*` (raw journals) and
  `logs/improvement/**` (mining state, unaccepted drafts, campaign
  ledgers, checkpoints, per-round decompositions, decision-record drafts).
  Nothing under `logs/improvement/` is ever committed by this loop.
- **Committed**: `docs/improvement/` holds only artifacts an operator has
  already reviewed — accepted `improvement-proposal/1` records and the
  `blocker-pattern/1` records they cite (sanitized, hash-referencing local
  journals). A round's decomposition JSON is committed too, but under its
  own campaign root inside `logs/improvement/campaigns/<id>/` (needed
  because `plan_approval.prepare_approval` requires the decomposition to
  already be a git blob at `base_commit`) — never under `docs/improvement/`.
- **Close-out**: a campaign's own drafted `docs/decisions/` record starts
  as a file under `logs/improvement/campaigns/<id>/decision-draft/`; an
  operator reviews it and lands it into the real `docs/decisions/` by hand.
  This loop never writes directly into `docs/decisions/`.

## 3. CI validation (`scripts/dev/check_improvement_artifacts.py`)

A stdlib-only, hand-written JSON Schema engine (no `jsonschema` dependency)
that walks every `*.json` file under a committed artifact tree (default
`docs/improvement/`) and validates it against its declared `protocol`
(`blocker-observation/1`, `blocker-pattern/1`, `improvement-proposal/1`).
Beyond schema shape it enforces three business rules by hand: an accepted
proposal must carry a human ruling; every `success_criteria` entry's
`file` must be a member of its own `required_paths`; every `pattern_ids`
citation must resolve to a real pattern record in the tree. Exit status is
0 only when the tree exists and every artifact validates cleanly.

`tests/test_improvement_closeout.py` exercises this checker directly
against the seeded `docs/improvement/` tree (expect exit 0) and against a
throwaway copy with a violation injected (expect exit 1) — run it (or the
checker CLI) before committing any change under `docs/improvement/`.
Where an improvement retires a workaround, `scripts/dev/check_workaround_
retirement.py`-style per-item checks prove it stays gone; that is a
per-proposal concern, not part of this generic artifact-tree gate.

## 4. Close-out promotion (`improvement_loop.draft_decision_record`)

Called automatically from `remeasure()`'s success branch once every seeded
key is `observed_fixed` or excluded (`waive`). Renders a
`NNNN-<slug>.md` from `docs/decisions/TEMPLATE.md` with `Concerns-paths:`
filled from the proposal's `target_surface` paths and a before/after
evidence summary appended to "Validation and reversal", numbered one past
the highest `NNNN-*.md` already in the real `docs/decisions/` — but always
written under the campaign root's own `decision-draft/`, never into the
real tree directly. An operator reviews the draft, flips its `Status:` to
`accepted`, and commits it by hand; only then does
`decision_registry.active_decisions_for_paths` start serving it back as an
`active-decision-notice` in future plan approvals touching those paths.
The cited patterns flip to `status: addressed` (stamped with the closing
`campaign_id` and `landing_commit`) in the same close.

## Hard rules (violations fail review)

1. No new `state-ledger` record kinds; no ledger writer changes.
2. No new `prepare_approval` / `issue_receipt` parameters; no new
   `canonical_plan_graph_payload` top-level keys.
3. No third-party dependencies — every checker here (`check_improvement_
   artifacts.py`) is hand-written stdlib.
4. The audit stage writes only under `logs/improvement/`; it never touches
   `docs/`, `schemas/`, `AGENTS.md`, or code. Implementation happens only
   inside a receipted PlanGraph node with its own scoped grants.
5. Analyzers propose; they never mutate an approved decomposition in
   place, and this loop never commits a decision record — only an
   operator does.
6. Observations carry normalized signatures and hash refs, never raw
   transcripts; committed pattern records are the sanitized projection.
