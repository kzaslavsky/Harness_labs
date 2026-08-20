# 0005 — Ledger-backed review/fix gate

Status: accepted
Concerns-paths: harness_labs/featurerun/review_fix.py, harness_labs/featurerun/feature_run.py
Date: 2026-08-04
Owners: harness controller

## Context

The Archimedes experiment required six separate FeatureRuns because each review
ended the run: a reviewer found a defect, but no controller-owned path repaired,
verified, and independently re-reviewed the same candidate. Simply repeating
review prompts is unsafe. Claude implement-v13 records finding identity across
cycles after observed runs repeatedly rephrased the same issue and grew fixes
beyond the planned surface.

## Decision

FeatureRun has an optional pre-commit review/fix gate. Its smallest stage remains
the existing `TaskAttempt -> Executor -> TaskResult` boundary. A stage factory
selects any backend for independent review, scoped repair, and verification.

The deterministic controller owns a durable `review-ledger/1`. It keys findings
by `(file, subject)`, collapses duplicates, preserves required dispositions,
constructs the exact fix list, checks fixer and verifier coverage, and decides
whether another review cycle is allowed. Reviewers and fixers provide judgment;
they do not own finding identity or stopping.

The first review is the only discovery pass. The controller freezes its finding
set. Later review passes close that set after targeted verification; a new
finding is recorded as deferred and cannot authorize additional work in the
current FeatureRun.

Lifecycle verification and adversarial review are separate gates. FeatureRun
runs the declared verification command itself and treats its exit code as
authoritative. Failure enters a bounded fixer in the same candidate worktree,
then the controller reruns the identical command. Adversarial review remains a
later discovery gate and cannot override verification evidence.

The port adopts these Claude implement-v13 controls as independent switches:

- re-raise ledger and within-cycle duplicate collapse;
- normative `protects` citation and fix-cost metadata;
- surface-expansion screening;
- a separate fixer with an explicit finding list;
- targeted verification and fix-regression re-review;
- deterministic mechanical/sensitive cycle limits;
- no-progress and marginal-yield exits; and
- an optional technical-debt sink.

The default differs deliberately from Claude: a required or contract finding is
never converted to debt at a budget boundary. The last permitted cycle is
review-only because applying a fix without budget to re-review it would create
an unaudited candidate.

## Alternatives

- Let one coordinator remember prior findings in its context. Rejected because
  compaction, restart, and reviewer rewording erase authoritative identity.
- Recursively re-run FeatureRun. Rejected because each run would create a new
  worktree and lose the candidate-local fix history.
- Port Claude's full prompt, panel, polling, and filesystem protocol. Rejected
  because those are provider/runtime mechanisms rather than the minimal
  platform-agnostic control contract.
- Always enable every guard. Rejected because controlled experiments need
  independently attributable policy changes and some repositories already
  provide equivalent gates.

## Evidence

- `tests/test_feature_run.py` proves the production FeatureRun path runs review
  before creating its candidate commit.
- `tests/test_review_fix.py` proves fix/verify/re-review closure, duplicate and
  scope screening, required-finding blocking, and independent policy switches.
- `tests/test_controller_live.py` proves a fixer may enter a dirty candidate
  while the controller receipts only the fixer's own path delta.

## Consequences

Review policy is explicit and auditable rather than hidden in prompts. Adding a
backend requires only stage executors; it does not change ledger semantics.
Review context grows with the ledger, so future optimization may project compact
closed entries after measured runs, but this decision does not add speculative
context compaction.

## Validation and reversal

Keep the gate while live runs close genuine findings without widening scope and
while audit verification reconstructs every cycle. Revisit default cycle/yield
values only from comparable empirical runs. Supersede the ledger key if measured
false merges show `(file, subject)` is insufficient; migrations must preserve old
keys and outcomes.
