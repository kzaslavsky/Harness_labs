# Flow Editor Convergence Application

First application of
[`convergence-campaign-plan.md`](convergence-campaign-plan.md):
domain `ui-fidelity`, product Retinology, target the Flow Editor design
mockup. Everything product-specific lives here; the campaign plan is
domain-neutral.

## Campaign parameters

- **Domain:** `ui-fidelity`.
- **Target:** `kind: "mockup_html"` —
  `Flow_Editor_work/Flow_Editor_13AUG26_UISTREAMLINE/mockup_v10.html`
  in the Retinology repo, snapshotted at campaign open. The mockup is
  inspectable HTML/CSS: the inspector reads it as source, not as
  pixels.
- **Seed audit:** `FLOW_EDITOR_VISUAL_FIDELITY_AUDIT_20260817.md`
  (commit `bcc0f4c`, currently branch-local — copy into the campaign
  root at open). Its 20 findings are round 1's audit output; no
  discovery pass is needed, so the measurer is not exercised until the
  round-1 post-repair audit.
- **Sanitizer hook:** Retinology's `scan_export_phi.sh`. Capture runs
  only against synthetic fixtures (the predecessor campaign's PL-4
  ruling); the capture script refuses a non-fixture database; finding
  text may cite selectors and token names, never rendered record
  content. Sanitizer failure is a campaign hard stop.
- **Repo identity:** `.harness/repository.json` exists only on the
  campaign branch lineage (commit `b2d8923`) — a base-selection
  constraint.
- **Branch model:** per-round planning artifacts live in the product
  repo under `Flow_Editor_work/fidelity/round-N/`, committed on a
  campaign branch forked from the audited base — never on the base
  lineage itself. One product commit per round is an approval
  precondition.

## CC-00 — Base establishment

The base does not exist yet: the predecessor campaign
(`flow-editor-uistreamline`) sealed 24/26 nodes but its release-gate
join (WP-25) never ran; the only integrated tree is a hand-inspected
preview worktree, which is audit evidence, not an adoptable base.

Decision required before anything is estimated:

- **Option A (recommended): join-only PlanGraph** over the four sealed
  leaf lineages (WP-13, WP-22, WP-23, WP-24) — controller-produced
  merge, conflicts through the registered join channel. Drops the
  unsealed WP-21 lineage and with it the stranded SSIM region gate
  (`tests/test_flow_visual_import_region.py`, commit `9588507`).
- **Option B:** finish WP-21 + WP-25 in the predecessor campaign —
  retains the WP-21 work and the SSIM gate at the cost of reviving the
  campaign.

Either way the predecessor graph must be settled (non-resumable) before
`campaign_opened`, and the chosen base commit, its merge-base with
product `main`, and the identity-bearing branch are pinned in the
campaign header.

## Measurer (CC-03 instantiation)

- **Capture** — `scripts/ui_fidelity_capture.py`: matrix =
  route × viewport × theme × **interaction**, where interactions are a
  declared ordered gesture script per route (palette click-add,
  keyboard add, native `DragEvent`+`DataTransfer` drop, `+ Add next`,
  save/reload, Run) — the audit's blocking defects were
  interaction-triggered network/console failures invisible to a static
  walk. Per cell: screenshot, DOM snapshot, computed styles for a
  declared selector list, ARIA snapshot, console log, network log.
  Stabilization borrows Playwright's hardened approach
  (`emulateMedia({reducedMotion})`, animation disabling,
  `document.fonts.ready`, pinned DPR). Playwright is declared in the
  product repo's venv, passed as an argument.
- **Inspector** — role instructions distilled from the seed audit's
  methodology: mockup as source; region-by-region sweep; five lenses
  (color grammar, layout/spacing, behavior, copy, accessibility);
  false-positive discipline; per-key verdicts per the campaign plan.
  Composed per round as a role/profile with the round's capture argv
  frozen into `preflight_argv`.

## Round 1 seed and decomposition sketch

**Seed transcription** (~0.5 day, operator, agent-assisted): the prose
audit cannot pass ingest validation (no `subject` slugs, no
`required_paths`), so its 20 items are transcribed into
`round-1/seed-findings.json` per the finding contract and sealed as
round 1's audit artifact. The transcription assigns the `required_paths`
the decomposition below derives from.

**Rulings** (audit's own nominees): items 14, 15, 18 (`desk`), plus the
item-12 disposition (`live` — needs the preview at the CC-00 base with
a completed pipeline run). Items 17 and 20 carry author-default
dispositions (accept both) the operator may veto.

**Repair nodes** (planner refines against the selected base; paths from
the audit's root causes; serialization per S1–S3):

| node | scope (audit items) | anchor paths | deps |
|---|---|---|---|
| R1-01 | functional authoring: 1, 2, 3, 6 | `retinology/web/_l2_pipelines.py`, `palette.js`, `canvas.js` | — |
| R1-02 | shared stage-token binding: 7 (all regions) | `flow_editor.css` ← `tokens.css` stage tokens | — |
| R1-03 | palette presentation: 4, 5 | `flow_editor.css`, `palette.js` | R1-02 (shared file) |
| R1-04 | Import region: 9, 10; verify item-8 fix on base | Import templates/CSS | R1-02 |
| R1-05 | Export presentation: 11; 12 per ruling | Export palette templates | R1-02 |
| R1-06 | small items: 13, 16, 19 + `require_repair` rulings | per ruling | R1-01 |
| R1-07 | join + regression gate: existing suites; assertions for every asserted confirmed-good entry; re-assertion of keys closed earlier this round | — | all |

No catch-all node; surprises block and route per the campaign plan.
Confirmed-good entries from the audit enter as `confirmed_good` only
with a machine-checkable assertion; hedged entries (the EV-CHROME-05
handle-drag clause, items 12 and 18) enter as `watch`.

**Expected outcome, stated plainly:** round 1's realistic result is a
measured handoff — the post-repair audit is the measurer's first real
run and doubles as its recall calibration (run once without the
exclusion list against the known seed findings). Closure within the
3-round bound is the optimistic case.

## Deterministic gates

Frozen at CC-00 against the selected base (verify each exists there
first):

```text
.venv/bin/pytest tests/test_l2_flow_editor.py tests/test_l2_flow_editor_palette.py tests/test_l2_dark_mode.py tests/test_flow_parity_oracle.py tests/test_current_ui_graph.py -q
.venv/bin/python scripts/dev/flow_editor_walk.py
python3 scripts/check_current_ui_graph.py
```

plus the full Retinology suite and repository-required documentation,
UI-graph, and PHI checks at the final gate.
