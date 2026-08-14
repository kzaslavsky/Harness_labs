# Analysis: Autonomous Maintenance Routines for Harness Labs and Retinology

Status: analysis note, 2026-08-14
Companion to: `boris-cherny-automation-routines.md` (the transcribed list)

Scope caveat: the Retinology repository could not be read from this session
(GitHub access is scoped to `kzaslavsky/harness_labs`). The Retinology
assessment below is grounded in
`docs/development/clinical-evidence-provenance-research-program.md`, which
describes Retinology as a provenance-preserving clinical data layer for
ophthalmology: AI extraction of structured observations from heterogeneous
clinical documents, longitudinal multimodal records (imaging,
electrophysiology, genetics), cohort construction with auditable lineage, and
inherited retinal disease as the initial domain. Where the assessment depends
on repository specifics it says so.

## 1. Critique of the list

### What the list gets right

1. **Closed predicates, not open goals.** Nearly every routine converts
   "improve the code" into a checkable claim: *this input crashes the app*,
   *this code is unreachable*, *this test cannot fail*, *this flag is at 100%*.
   A routine with a machine-checkable definition of done can run unattended
   because its PRs carry their own evidence. This is the same principle as the
   Harness Labs contract: no claim without a verifiable artifact.
2. **Deletion bias.** Seven of the eleven routines primarily remove code
   (dead code, useless tests, duplicate implementations, stale flags, unused
   features, excess abstraction layers). This is the correct counterweight to
   the dominant failure mode of frontier coding models — accretion and
   overengineering — which Harness Labs' README names explicitly as motivation.
3. **Targets orphaned work.** Flaky tests, stale flags, and forgotten internal
   features are debt classes with no natural human owner. Standing agents are
   a better fit than sprint planning for work nobody is accountable for.

### Where the list is weak

1. **It mixes two very different verification regimes without saying so.**
   - *Oracle-backed*: crash fuzzer, dead-code removal, useless-test pruner,
     shipped-feature inliner, flaky-test fixer, abstraction police (given a
     rule file). These can be trusted roughly in proportion to their proof
     artifacts.
   - *Taste-backed*: logic simplifier, abstraction improver, dup unifier
     (partially), ant-only shipper's "ship" half. "Simpler" and
     "over-engineered" are judgments; an agent that files such PRs at scale
     produces churn, taste disputes, and reviewer fatigue. These routines need
     a human-approval gate and conservative thresholds, and the list does not
     distinguish them from the oracle-backed ones.
2. **Hidden telemetry dependencies.** Ant-only shipper needs usage analytics;
   shipped-feature inliner needs flag-rollout state; flaky-test fixer needs CI
   history. These routines are only portable to organizations that already have
   that data plane. The list implicitly assumes Anthropic-scale infrastructure.
3. **"Provably unreachable" is doing heavy lifting.** In dynamic languages,
   reflection, plugin registries, serialization hooks, and FFI make
   reachability undecidable in general. A safe dead-code routine must emit a
   proof artifact (static call graph + coverage/telemetry corroboration +
   grep for dynamic references), not model confidence, and must prefer
   "quarantine then delete" over immediate deletion.
4. **Ship-or-delete is a product decision.** The ant-only shipper is the one
   routine whose output is not a code-quality claim but a product judgment.
   Automating the *evidence gathering* (usage counts, owner, age) is sound;
   automating the *decision* is not.
5. **Review economics are unaddressed.** Eleven routines opening PRs is a
   denial-of-service attack on human reviewers. A fleet needs: per-routine PR
   budgets, confidence-ranked queues, dedup across routines (dup unifier and
   abstraction improver will collide), auto-close on staleness, and a measured
   merge rate. The honest success metric is merged-PR rate and
   escaped-defect rate per routine — not PRs opened, which is a vanity metric.
6. **Routines interfere.** The simplifier and the bugfixer racing on the same
   file, or the abstraction improver flattening a layer the abstraction police
   just enforced, produce conflicting PRs. The fleet needs serialization by
   file ownership and a shared view of in-flight changes — which is precisely
   an orchestration-harness problem (see §4).
7. **The list is code-shape-centric.** Nothing addresses doc/spec drift
   (documentation asserting behavior the code no longer has), dependency risk,
   or security posture — classes of debt that are at least as automatable and
   often higher value. For an evidence-driven repo, a "contract drift auditor"
   (do the docs, schemas, and code still agree?) is a conspicuous omission.

## 2. Speculated implementations

Plausible mechanism per routine, assuming a Claude-Code-style agent loop with
repo access, test execution, and PR authoring:

- **Crash fuzzer** — Generate structured/property-based inputs against public
  entry points (CLI args, API payloads, file parsers); triage crashes by
  deduplicated stack signature; minimize the repro; bisect to the introducing
  commit; open a PR containing the failing test *and* the root-cause fix.
  Oracle: the new test fails before the patch and passes after.
- **Ant-only shipper** — Query flag/usage telemetry for internal-only gates
  older than N months; rank by usage; for near-zero usage propose deletion,
  for real usage propose promotion; attach the evidence table; require human
  sign-off for either action.
- **Logic simplifier** — Shortlist functions by cyclomatic/cognitive
  complexity and churn; rewrite; prove behavior preservation by the existing
  suite plus generated property tests (or exhaustive input enumeration for
  small domains); reject its own patch when equivalence cannot be demonstrated.
- **Logic bugfixer** — Extract a decision-dense region into an explicit model
  (truth table, state machine, or property set); model-check or
  exhaustively test the model against the implementation; each divergence is
  either a bug (fix code) or a spec gap (file question). The interesting move
  is that *modeling*, not reading, is the bug-finding instrument.
- **Dup unifier** — Detect near-duplicates by normalized-AST hashing or
  embedding similarity; pick the survivor by test coverage and call-site
  count; migrate call sites; delete the rest. Risk: near-duplicates that
  differ deliberately (subtle domain variants) — requires a semantic diff of
  the variants' behavior, not just similarity.
- **Dead-code removal** — Static reachability from all entry points, minus
  dynamic-reference grep, cross-checked against runtime coverage or telemetry
  where available; emit the proof artifact into the PR; stage as
  deprecation-then-delete.
- **Useless-test pruner** — Mutation testing: a test that kills zero mutants
  of the code it claims to cover cannot fail for a real reason. Also catches
  tautological asserts, tests fully neutralized by mocking, and tests of
  deleted features. Oracle is crisp and machine-checkable.
- **Shipped-feature inliner** — Query flag state; for flags at 100% for N
  days, inline the enabled branch, delete the disabled branch and the flag
  registration; verify by full suite. Nearly mechanical when a flag registry
  exists.
- **Flaky-test fixer** — Mine CI history for tests with intermittent
  pass/fail on identical commits; reproduce under stress (reordering,
  parallelism, time/network/filesystem perturbation); classify the
  nondeterminism source; fix the root cause (shared state, sleeps, order
  dependence) rather than adding retries; prove by N consecutive stressed runs.
- **Abstraction improver** — Detect single-implementation interfaces,
  pass-through wrappers, config that is never varied, and depth-N indirection
  chains; inline them; taste-backed, so ship with conservative thresholds and
  human review.
- **Abstraction police** — The most mechanical of all: a declared layering
  rule file plus an import checker; on violation, move code or invert the
  dependency. Harness Labs already has the detection half in
  `scripts/dev/check_import_boundaries.py` and
  `tests/test_import_boundaries.py`; the routine is "auto-fix on red."

## 3. Which routines are most useful

### Generally

Ranked by (value × trustworthiness of the oracle) ÷ infrastructure required:

1. **Flaky-test fixer** — flaky CI is a tax on every merge and erodes trust in
   the only verification signal an autonomous fleet has. Fixing it compounds:
   every other routine depends on a trustworthy suite.
2. **Crash fuzzer** — finds real defects with an unambiguous oracle and ships
   the repro alongside the fix.
3. **Useless-test pruner** — mutation testing gives it the cleanest evidence
   artifact on the whole list, and vacuous tests are epidemic in LLM-written
   code: agents optimizing "tests pass" naturally produce tests that cannot
   fail.
4. **Dead-code removal / shipped-feature inliner** — high value, near-mechanical
   where the proof or the flag registry exists.
5. Taste-backed routines (simplifier, abstraction improver, dup unifier) —
   real value but require human gates; net value depends on review capacity.

### For Retinology

For a provenance-preserving clinical data platform, the ranking shifts
sharply toward silent-corruption defenses:

1. **Logic bugfixer.** Eligibility criteria, phenotype normalization, cohort
   inclusion, laterality/temporal logic — clinical rule code is exactly
   "tricky logic," and its failure mode is not a crash but a silently wrong
   cohort, which is the worst outcome for a project whose entire thesis is
   evidentiary credibility. Modeling the rules explicitly (truth tables,
   property sets) and diffing model against implementation doubles as
   documentation — an executable spec per clinical rule, congruent with the
   evidence-manifest ethos.
2. **Crash fuzzer.** The extraction pipeline ingests the messiest input class
   imaginable: scanned faxes, malformed PDFs, inconsistent device exports,
   partial records. Fuzzing parsers with corrupted/truncated clinical-document
   shapes hardens the front door.
3. **Useless-test pruner.** False confidence from vacuous tests is worse in a
   clinical context than in a normal product; a test suite whose every test is
   demonstrated capable of failing is close to a regulatory asset.
4. Taste-backed routines rank low: churn in a codebase that must maintain a
   stable audit trail has negative expected value.

The list also suggests a domain-specific twelfth routine Retinology should
want: a **provenance auditor** — a standing agent that samples emitted
observations and verifies each still traces to its source artifact through
the declared chain (extraction → normalization → cohort → result). That is the
ant-only shipper's *evidence-gathering* pattern pointed at data lineage
instead of feature usage.

### For Harness Labs

1. **Abstraction police** — already half-implemented here
   (`scripts/dev/check_import_boundaries.py` guards the
   core/featurerun/plangraph/observability/graphrun layering). Completing the
   loop (auto-fix on violation) is cheap and exercises the repo's own
   FeatureRun machinery on itself.
2. **Abstraction improver / logic simplifier** — this repo's stated motivation
   is taming model overengineering, and its AGENTS.md carries explicit
   CRITICAL warnings against unbounded refactors. An improver bounded by
   those same contract limits is the repo eating its own cooking.
3. **Dup unifier** — rapid agent-generated growth (the implement-v13 document
   family, repeated executor/backend patterns) is exactly the substrate where
   near-duplicates accumulate.
4. **Useless-test pruner** — most tests here are agent-written; mutation
   testing is the cheapest way to find which of them actually constrain
   behavior.

The deeper point: Harness Labs should not just *run* these routines — it is
the natural **host** for them. Each routine is a FeatureRun with a fixed
policy (a static-plane document defining its predicate, evidence requirements,
and PR budget) dispatched on a schedule by PlanGraph. Boris's list is less a
tools list than a product roadmap for GraphRun: eleven standing policies over
one harness, inheriting worktree isolation, evidence journals, verification
gates, and guarded merges that the harness already provides. The fleet-level
problems the list ignores (PR budgets, interference, merge-rate metrics) are
precisely PlanGraph's admission, scheduling, and observability concerns.

## 4. What to build first

**Build the useless-test pruner first**, as a FeatureRun policy.

- **Cleanest oracle on the list.** Mutation testing yields a machine-checkable
  proof ("this test killed zero mutants; here are the mutants") that drops
  straight into the run journal — a perfect match for the evidence-driven
  contract, with no judgment calls in the deliverable.
- **Deletion-only and bounded.** Small reviewable diffs, trivially
  revertible, no behavior change by construction. It fits AGENTS.md's
  "bounded work only" constraint better than any other routine.
- **Zero external dependencies.** No flag registry, no usage telemetry, no CI
  history required — only the repo and its test runner. That keeps it
  platform-agnostic, matching the Harness Labs goal, and makes it immediately
  reusable on Retinology.
- **Serves both codebases now.** Both repos' tests are substantially
  agent-written, and vacuous tests are the canonical LLM failure mode; in
  Retinology they are actively dangerous, in Harness Labs they corrupt the
  accuracy gate that every run depends on.
- **It hardens the verification signal before other routines rely on it.**
  Like the flaky-test fixer, it improves the suite itself — the correct first
  investment for a fleet whose every other member trusts "tests pass."

Concrete first milestone: a `test-pruner` policy document (predicate: killed
zero mutants under `mutmut`/`cosmic-ray` over its covered module, plus
tautology detection; evidence: mutation report artifact; budget: ≤5 deletions
per run) executed by `run_feature_worktree(...)`, with the mutation report
attached to the run's `logs/runs/<run-id>/` journal and a human-reviewed PR as
output. The flaky-test fixer is the natural second build once the repo
accumulates enough CI history to mine, and the abstraction police auto-fixer
is the cheapest quick win alongside either.
