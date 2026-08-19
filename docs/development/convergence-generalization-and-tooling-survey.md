# Convergence generalization discussion + delta-tooling survey

Date: 2026-08-18. Two Opus agent reports commissioned as companions to
`mockup-delta-convergence-plan.md`: (1) whether the convergence loop
generalizes into a goal-agnostic PlanGraph orchestrator, (2) whether
existing frameworks/SSIM/delta tools already cover the measurer role.

## Synthesis (read this if nothing else)

Both reports converge on one theme from opposite directions: **the
judgment agent is the right measurer, and everything cheap and
deterministic should be arranged around it to keep it honest.**

- The generalization is real and the seam is already half-cut: the finding
  envelope, path-routed ownership, `fix_claimed`-vs-observed-`fixed`,
  typed rulings, the stall predicate, and the driver are domain-neutral;
  only the capture matrix, lenses, and PHI clause are UI-shaped. The
  domain filter is: **the delta must localize to file paths** (PlanGraph
  ownership is path-granted). Migration completeness is the recommended
  second domain; performance budgets and test coverage are poor fits.
- Five near-zero-cost choices now keep generalization cheap later:
  `.convergence-campaigns/` (not `.fidelity-campaigns/`) as the durable
  directory name; `target:`/`target_amended` (not mockup-named) header
  records with a `kind` field plus `domain` in `campaign_opened`; the
  driver's measure step behind one named function boundary from
  generation 1; the invariant `file ∈ required_paths`, ownership derives
  from `required_paths` alone, stated in the finding contract; the PHI
  scan as a named `pre_journal_sanitizer` config hook. Build no Measurer
  base class, no plugin registry, no second domain until campaign 1
  terminates.
- Parallelism: parallel generations of one campaign are incoherent;
  parallel *campaigns* need one small primitive — an advisory path-lease
  journal in the join-conflict-store pattern (~200 lines), plus a
  `finding_referred(to_campaign, key)` record (transfer, one level up).
  But the true ceiling is operator ruling throughput, so the first
  parallelism work is reducing rulings per generation (author-default
  dispositions, standing waivers, batched packets), not a scheduler.
- Biggest generalization risk: `amend_criterion` + empty-delta
  termination lets a loop converge by moving the goalposts. Guard: an
  **amendment ratio** derived view in the ledger, printed at every
  termination, success requiring explicit operator acknowledgment above a
  threshold.
- The tooling survey confirms the gap: every visual-regression product
  compares the app against a *prior screenshot of itself*; the
  design-as-baseline line (Applitools Figma plugin, OverlayQA, academic
  GVT/GUIPilot) stops at annotated image regions. **Nothing ships
  file-rooted, repair-path-bearing, disposition-aware findings from a
  mockup-as-source comparison with interaction/network coverage.** The
  agent approach is not reinventing an existing product.
- Hybrid additions worth building into C2: (1) odiff/pixelmatch
  cross-generation per-cell triage (~0.5 day — tells the inspector where
  to look, makes "dry" partially machine-checkable); (2) Playwright ARIA
  snapshots per cell (~0.5–1 day, zero new deps — deterministic semantic
  layer, powers the a11y lens); (3) optionally a Design2Code-style
  block-match scalar per generation as an agent-independent convergence
  signal. Borrow Playwright's stabilization tricks rather than re-derive
  them. Post-convergence, freeze the win with `toHaveScreenshot` — a
  same-app baseline is finally trustworthy then. Avoid hosted VRT SaaS
  during the campaign (wrong oracle; PHI friction).

---

## Report 1 — Generalizing into a convergence orchestrator (Opus)

### The seam is already half-cut, and it isn't where the plan draws it

The plan presents itself as a UI-fidelity document, but the load-bearing
parts are not UI-shaped and were not invented by it.
`harness_labs/featurerun/review_fix.py` already treats a finding as a
record with `file` and `required_paths` and derives routing by
intersecting `required_paths` against `allowed_paths`. The harness's
delta contract is domain-neutral today.

Inventory:

- **Already domain-neutral, no work needed:** finding envelope,
  `required_paths`-driven ownership, transfer to path-granted
  descendants, deterministic join, digest-frozen plans, per-node operator
  relief, budget lineages.
- **Domain-neutral in the plan but not yet extracted:** `fix_claimed` vs
  observed-`fixed`; the three typed dispositions; the stall predicate;
  generation bounds; the base-adoption rule; seal-then-ingest
  idempotence; planning-time disjointness with a broad-grant sink.
  `fix_claimed` is a general claim about agent self-report — success
  signals are evidence of effort, not effect — and belongs in the harness
  proper.
- **Genuinely UI-shaped:** capture matrix axes, the five lenses,
  fonts/DPR/motion gating, the PHI clause, the mockup's HTML-ness.

A generalized campaign needs five contracts; two are hard:

- **Target:** a pinned digest-addressable reference plus an amendment
  protocol that names an invalidation scope
  (`target_amended(digest, invalidates: <key predicate>)`). The predicate
  is the whole contract.
- **Delta:** exists; per-domain piece is the `details` block and key
  derivation. Constraint: **the delta must localize to file paths** or it
  cannot own a repair node.
- **Measure:** the capture/inspect split is the generalization —
  deterministic evidence acquisition with honest coverage, then keyed
  findings over that evidence.
- **Ruling channel:** keep exactly three dispositions.
- **Ledger + driver:** generalize as-is.

### Candidate domains, ranked

- **Migration completeness — best fit, recommended domain 2.** Measurer
  is a grep/AST sweep; key `(file, symbol)`; deterministic, monotone,
  afternoon-sized. Rulings map cleanly (`waive` = site stays;
  `amend_criterion` = migration target changed).
- **Accessibility conformance — near-best, reuses C2.** Rule engine over
  the same capture matrix; key `(owning_file, rule_id)`;
  waive-with-justification is already industry practice. Also provides a
  deterministic cross-check on the judgment inspector.
- **API contract conformance — good; stress-tests `amend_criterion`**
  (half of real violations are the spec being wrong).
- **Doc-code drift — good, structurally interesting:** the target is a
  *relation* between two artifacts, no pinned reference; the one domain
  that forces a genuine Target-contract change. Do eventually, not
  second.
- **Security-finding closure — fits, but the measurer is third-party:**
  ingest must adapt scanner ids into keys; `waive` carries compliance
  weight and needs an identity field.
- **Performance budgets — poor fit, don't.** Findings often have no file
  (ownership collapses) and benchmark variance makes "open in two
  generations" the normal state (every campaign stalls) without a
  significance gate.
- **Test-coverage closure — poor fit, instructively.** Keys
  `(file, uncovered_region)` churn under edit; coverage is a proxy
  target, and a convergence loop pointed at a proxy converges on the
  proxy.

### Parallelism: one primitive, and it isn't a scheduler

Parallel generations within one campaign are incoherent. Real cases:
parallel campaigns over disjoint surfaces; parallel goals over a shared
base with terminal join.

What breaks: path custody has no cross-lineage arbiter (disjointness is
enforced within a plan digest only; everything durable is lineage-scoped
— join resolutions, budgets). The repo prefigures the *pattern*, not the
primitive: an advisory `.plan-graph-path-leases/` journal in the
join-conflict-store mold (append-only JSONL, flock+fsync, ~200 lines),
leases acquired at `campaign_opened` over the union of planned
`allowed_paths`, denied on overlap. Cross-campaign join is made
unnecessary rather than smarter: shared merge-base plus disjoint leases
reduces it to a mechanical tree union. Finding keys are global, so a
referral record is needed — `finding_referred(to_campaign, key)` — the
transfer rule one level up. The worktree-only discipline is this lease
applied manually at repo granularity.

The actual ceiling is operator attention: rulings, plan reviews,
approvals, and block interventions are serial in one human. The first
parallelism work is reducing rulings per generation (author-default
dispositions the operator vetoes, policy-level standing waivers, batched
packets) — build the lease store when two campaigns are actually ready,
not before.

### Sequencing: five choices now, and what not to build

Stays UI-specific in campaign 1: C1, C2, the lenses, the mockup's
HTML-ness, the PHI scan's content. Do not build a Measurer base class, a
plugin registry, target-kind dispatch, or the deferred schema registry.
No second domain until campaign 1 reaches a real termination.

1. Domain-neutral names for durable on-disk things:
   `.convergence-campaigns/`, neutral protocol strings (module names are
   a sed away; journal strings are not).
2. `target: {kind, digest, snapshot_path}` and `target_amended`, plus
   `domain:` in `campaign_opened`.
3. Split the driver at the measure seam in generation 1 — one named
   function returning a sealed artifact digest; gen 1's implementation
   reads the transcribed seed. The crash-re-entry logic is built around
   this boundary; retrofitting is expensive.
4. Keep the field `file` but state the invariant: `file ∈
   required_paths`; ownership derives from `required_paths` alone.
5. PHI scan as a named `pre_journal_sanitizer` hook in campaign config.

Keep the three dispositions closed.

### The biggest risk

A convergence loop makes goal mis-specification cheap to sustain and
expensive to notice: termination is "the measurer found nothing new," and
the measurer is calibrated against the finding set the loop itself
produced — a systematic blind spot is invisible to that calibration by
construction. Generalization worsens this via `amend_criterion`: an
amendable target plus empty-delta termination can converge by moving the
goalposts, recorded as success. In UI a human's taste bounds it; in
security/performance it is the failure mode. Guard (build into C3 now):
an **amendment ratio** derived view — keys closed via `amend_criterion`
over keys closed total — printed at every termination, success requiring
explicit operator acknowledgment above a declared threshold. The rest of
the design distrusts agents' claims of repair; this points the same
distrust at the target.

---

## Report 2 — Delta-tooling landscape survey

### Category findings

**Pixel/perceptual regression** (pixelmatch, odiff, resemble.js,
Playwright `toHaveScreenshot`, BackstopJS; hosted: Percy/BrowserStack,
Chromatic, Applitools Eyes, Argos, Lost Pixel): every tool compares the
app against a **prior approved screenshot of the same app**. None answer
"does this match the mockup?" — only "did this change since the last
approved run?" Their false-positive controls assume near-identical
rendering pipelines, which mockup-vs-app violates by construction. Useful
only as a cross-generation regression tripwire.

**Design-to-code specialists:** Applitools' Figma plugin (successor to
the 2023 "Centra" waitlist product) is the closest productized
design-as-baseline system — Figma frames as Eyes baselines, Visual AI
region annotations — but output is dashboard annotations, not
file-keyed findings; Figma-input (screenshotting our HTML mockup would
discard its inspectable-source property); SaaS; AI-quality claims
unverified. OverlayQA ($39/mo) overlays Figma frames on live URLs and
claims per-issue CSS selectors — human-driven, unverified. Figma Dev
Mode/Code Connect are handoff, not verification. Academic line: **GVT**
(ICSE 2018; mockup model vs implementation model, taxonomy-classified
violations, 98%/96% precision/recall, deployed at Huawei) validates the
C1/C2 architecture shape but is Android-only and pre-LLM; **GUIPilot**
(FSE 2025) adds behavior mismatches; **Owl Eyes** (ASE 2020) finds
glitches without a mockup; **Design2Code** (NAACL 2025) contributes
reusable mockup-vs-render metrics (CLIP similarity + block match) usable
as a per-generation convergence scalar, not a finding generator.

**DOM/structural diffing:** Playwright ARIA snapshots
(`toMatchAriaSnapshot`) are the standout — YAML accessibility trees,
stable across CSS noise, and the expected tree is *derivable from the
mockup HTML by an agent*. No packaged computed-style differ exists; C2's
design is state-of-practice there, not behind it.

**Behavioral delta:** all existing tools (Storybook play functions,
Meticulous replay, model-based testing, LLM QA SaaS) check against
hand-written tests, recorded prior behavior, or authored models — none
against a design artifact, and none combine gesture scripts with
console/network capture and judgment. That combination exists only in
this plan.

### Honest comparison

Dedicated tools beat the agent on: determinism (same inputs, same answer,
forever), speed/cost per run (seconds and free vs. minutes-to-hours of
frontier tokens), no hallucination, legible tolerance knobs, mature CI
ergonomics.

The agent does what no listed tool does: mockup-as-*source* comparison
with zero baseline (extracts the intended token, not just "pixels
differ"); root-causing to `file` + `required_paths`; intent-vs-accident
judgment including "the mockup is wrong" (every baseline tool treats the
baseline as ground truth by construction); interaction +
console/network failure diagnosis against design intent — the audit's
dominant defect class; keyed finding identity across generations.

Where the plan should borrow rather than re-derive: Playwright's
stabilization hardening (reduced-motion emulation, font readiness,
animation disabling), and a cheap deterministic pre-filter in front of
the expensive sweep.

### Hybrid recommendations (all local, permissive licenses)

1. **odiff/pixelmatch cross-generation triage inside C2** (~0.5 day):
   per-cell changed-pixel ratio vs. the prior generation's same cell,
   written into the receipt. Focuses the inspector; makes "dry" partially
   machine-checkable ("zero pixel delta since the repair" is strong
   evidence a fix didn't land).
2. **ARIA snapshot per cell** (~0.5–1 day, zero new dependencies):
   deterministic semantic layer; the inspector compares against the
   structure implied by the mockup HTML — `C+S`-grade a11y evidence.
3. **Design2Code block-match scalar** (1–2 days, optional/deferrable):
   an agent-independent convergence trend per generation, sanity-checking
   "zero new findings" termination alongside the recall calibration.

Do not adopt during the campaign: hosted VRT SaaS (wrong oracle, PHI
friction, fixtures-only rule nullifies CI value), the Applitools Figma
plugin, OverlayQA. Post-convergence: freeze the converged state with
Playwright `toHaveScreenshot` as a regression net — at that point a
same-app baseline finally exists and is trustworthy.

### Verdict

The gap is real. Baseline tools solve "did it change?"; the
design-as-baseline line stops at annotated regions; nothing ships
file-rooted, repair-path-bearing, disposition-aware findings from
mockup-as-source with interaction/network coverage. The agent approach is
not reinventing an existing product — but it should embed the
deterministic layers above as cheap tripwires, both to focus the
expensive sweep and to keep the agent honest.

(Source URLs are preserved in the session transcript; key anchors:
Playwright ARIA snapshot docs, Applitools Eyes Figma plugin docs, GVT
arXiv 1802.04732, GUIPilot doi 10.1145/3728909, Design2Code NAACL 2025.)
