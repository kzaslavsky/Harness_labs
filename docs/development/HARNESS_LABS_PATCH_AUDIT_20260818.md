# harness_labs platform-patch audit — 2026-08-18

## What this document is

A merge-decision audit of every **platform-code** (`harness_labs`) change produced over the
course of the Retinology Flow-Editor streamline campaign, plus the two unmerged feature
branches and one dashboard branch that grew out of it. Its purpose is to decide what lands
on `main` and in what order.

This is the **second pass**. A first audit was produced earlier (2026-08-17) covering P1–P11.
Everything in that audit has been independently re-verified against the repository as it
stands now — commit hashes re-resolved, diffs re-read, tests re-inspected, branches
re-checked — rather than transcribed. Where a prior claim could not be re-verified, or where
the evidence has changed, this document says so explicitly.

**What is new in this pass:** a twelfth patch, **P12** (join-conflict resolution store), which
did not exist in reviewable form at the time of the prior audit (its branch existed with zero
commits ahead of the campaign tip). It is now complete, merged into the campaign branch, and —
uniquely among everything audited here — **has been exercised live, three times, on the real
running campaign**. That evidence is documented in full in §P12.

### Scope and coordinates

| | |
|---|---|
| Produced | 2026-08-18 |
| Primary branch | `claude/retinology-flow-editor-graphrun-5dd0de` @ `deb8c73` |
| Merge base | `main` @ `b5a39b1` (2026-08-14) — unchanged since the prior audit |
| Other branches audited | `dashboard_improve` @ `a1c5069`, `feature/plan-graph-attempt-carryover` @ `c4550ba`, `feature/plan-graph-continue-after-block` @ `c4007e0`, `claude/join-conflict-resolution-store` @ `deb8c73` (identical to campaign tip) |
| Method | read-only (`git log/show/diff/merge-tree`, file reads); no checkouts, no merges, no writes to any worktree |

**Hash stability note.** All 23 commit hashes cited by the prior audit still resolve and still
carry the same subjects. No branch cited by the prior audit has been deleted. One correction:
the prior audit described commit `095fb22` as the "repair_selection reuse-ordering fixpoint
fix"; its actual subject is `Fix repair_selection ordering bug; widen transient recovery
signatures` — it carries a small second change (transient signatures in `feature_run.py`,
+7 lines) alongside the audit fix. This does not change the recommendation but it does mean
P2 and P3 are not as cleanly separated as the prior audit implied.

### Classification vocabulary

- **Generalizable** — platform-level, no campaign content; belongs on `main` on its merits.
- **Run-specific** — hardcoded to this campaign (paths, node ids, runner internals); does not
  belong on `main` as written, though the *idea* may.
- **Mixed** — generalizable substance wearing campaign-specific clothing (name, location,
  fixture), or generalizable code with a coupling that must be understood before merging.

---

## P1 — Deterministic default RecoveryAgent + admission sibling-overlap warnings

**Commits:** `72bbdfa`, `857734b` (campaign branch)
**Files:** `harness_labs/featurerun/feature_run.py` (+53), `harness_labs/plangraph/plan_approval.py`
(+114, then +16), `harness_labs/__init__.py`, `tests/test_feature_run.py` (+72),
`tests/test_plan_approval.py` (+100), `experiments/run_flow_editor_streamline_plan_graph.py` (+8)

**Classification: generalizable (both halves).** Re-verified by reading the diffs. The recovery
agent (`deterministic_recovery_agent`, `feature_run.py:472`) is pure string classification over
failure text — no campaign vocabulary. The overlap analyzer (`_sibling_overlap_warnings` /
`_paths_overlap`, `plan_approval.py:448–458`) is a graph-topological analysis over the plan's
declared `allowed_paths`; it has no knowledge of what the plan contains. `857734b` adds only a
severity split (high = same file-like path declared by both siblings) and high-first sorting.

**Evidence.** 172 lines of new unit tests across the two test files. Additionally validated
against the real 26-node plan at prior-audit time (119 warnings, 17 high; correctly predicted
two join conflicts and a six-sibling shared-CSS cluster) — that live validation is second-hand
(prior audit) and I have not re-run it, but it is corroborated after the fact by P12: the
WP-25 join conflicts that actually materialized (`flow_editor.css`, `canvas.js`,
`test_flow_parity_oracle.py` shared between siblings) are exactly the class of overlap this
analyzer flags. That is meaningful independent corroboration of the analyzer's predictive value.

**Merge recommendation: merge now.** Best-quality change in the set alongside P2. Consider
splitting the commit: the overlap warnings are unambiguously additive (warnings only, no gate),
whereas flipping `recovery_agent`'s default from `None` to `deterministic_recovery_agent`
(`feature_run.py:694`) changes behaviour for every existing caller and deserves a release note.
The blast radius is bounded by `recovery_limit`. **See P5 — the P1 default is currently
silently overridden for PlanGraph-bound runs; that must be resolved before or with P5.**

---

## P2 — `repair_selection` reuse-ordering fixpoint fix

**Commit:** `095fb22`
**Files:** `harness_labs/plangraph/plan_graph_audit.py` (+45/−18),
`harness_labs/featurerun/feature_run.py` (+7), `tests/test_plan_graph_reuse_chain.py` (+69)

**Classification: generalizable.** A single-pass reuse-selection bug replaced with a
changed-fixpoint loop. No campaign coupling.

**Evidence.** 69 lines of new tests in a dedicated file. The session journal attributes roughly
nine redundant node re-runs to this bug prior to the fix (second-hand; not independently
re-derived in this pass).

**Caveat found in this pass:** the commit also widens transient recovery signatures in
`feature_run.py` (+7). It is a small, benign rider, but reviewers expecting a single-concern
commit should know it is there — and it overlaps in kind with P3.

**Merge recommendation: merge now, first.** Highest value / lowest risk in the set. It was
costing real compute on every resume.

---

## P3 — DNS / websocket transient recovery signatures

**Commit:** `3ab6a86`
**Files:** `harness_labs/featurerun/feature_run.py` (+7)

**Classification: generalizable.** Seven lines adding failure signatures to the deterministic
recovery agent.

**Evidence.** No test added. Justified by a specific live incident (a DNS blip during
attempt-41 on WP-13). Given the change is a widening of a string-match list with no branch
logic, the absence of a test is a defensible (if not ideal) call.

**Merge recommendation: merge now, with P1.** It is only meaningful once P1's agent is the
default.

---

## P4 — Verification-image capture and forwarding

**Commit:** `90afe02`
**Files:** `harness_labs/core/verification_images.py` (new, 342 lines),
`harness_labs/core/controller_live.py` (+46), `harness_labs/core/claude_task_executor.py` (+15),
`harness_labs/featurerun/feature_run.py` (+78), `tests/test_verification_images.py` (+236)

**Classification: mixed.** No campaign strings, but soft-coupled to pytest: it appends
`--basetemp` only to argv it recognizes as pytest (`_is_pytest_argv`), parses pytest's
`FAILED …` summary lines to identify failing tests, reconstructs pytest's own
`tmp_path`-directory naming rule (`re.sub(r"\W", "_", name)[:30]` plus ordinal), and assumes a
`*-diff.png` / `*-actual.png` / `*-reference.png` filename convention to rank images.

**WEAK POINT (re-verified in this pass — the prior audit's finding is confirmed, and is worse
than stated).** `test_failure_images_are_persisted_and_scoped_to_failing_tests` is vacuous.
The fixture (`_basetemp`, test file lines 60–79) creates directories named
`test_import_region_matches_de0` / `…_de1`. The code derives the prefix from the failing node
id `tests/test_visual.py::test_import_region_matches[desktop]` as
`test_import_region_matches_des` (26 chars + `_des` = the 30-char truncation). `…_de0` does not
start with `…_des`, so `scoped` is empty and `_select_images` falls through its documented
escape hatch (`pool = scoped or candidates`, `verification_images.py:201`) to the *whole* tree.
The test's `assert not any("unrelated" in value …)` then passes only because the six-image
limit is exhausted by the two failing directories' three images each before the round-robin
reaches the passing test's directory. **The scoping code path is therefore not exercised by any
test at all**, and the fixture — whose own comment claims to reproduce pytest's layout — does
not reproduce it. Raising `limit` to 7 breaks the test.

Additional gaps, all re-confirmed: the Codex `-i` argv construction is untested (asserted only
in the commit message); the Claude-executor path (the worker is told to `Read` absolute image
paths) has no verification that it works end to end; capture is **on by default** with only an
env kill switch (`CAPTURE_ENV_VAR`); there is no audit event when images attach and no spend
accounting; and the artifact manifest re-hashes the growing PNG set on every manifest
write/verify.

**Merge recommendation: hold, then merge with changes.** Required before merge: (1) fix the
fixture so the scoping path is actually exercised, and add a test that a passing test's images
are excluded *with the budget raised above the failing tests' image count*; (2) add a test for
the Codex argv construction; (3) reconsider on-by-default, or at minimum add an audit event so
the spend is visible; (4) verify or drop the Claude executor path.

---

## P5 — Review continuation

**Commits:** `09627c5`, merges `e03b31e` / `f6aff43` (originated on
`claude/featurerun-reexecution-plangraph-433b0e`)
**Files:** `harness_labs/featurerun/feature_run.py` (+135),
`harness_labs/featurerun/feature_run_policy.py` (new, +64),
`harness_labs/featurerun/review_fix.py` (+116), `harness_labs/plangraph/plan_graph.py` (+97),
`harness_labs/plangraph/plan_graph_audit.py` (+14), `harness_labs/__init__.py`, four
`experiments/run_*_plan_graph.py` launchers, `tests/test_feature_run.py` (+225),
`tests/test_review_fix.py` (+174), `tests/test_plan_graph.py` (+72)

**Classification: generalizable in substance.** `standard_review_continuation_recovery_agent`,
`ReviewFixResult.stop_reason`, `RecoveryContext.stage_detail`, ledger resumption, and
self-carried finding obligations are all platform concepts with no campaign content.

**Evidence.** ~470 lines of new test coverage, but entirely mocked. No live evidence at prior
audit time and none found in this pass.

**Both defects re-verified in this pass; both still present at `deb8c73`.**

**(a) It silently overrides P1's default.** `feature_run.py:1399–1411`: when
`recovery_agent` is absent from `feature_run_options`, the PlanGraph-bound path binds
`standard_review_continuation_recovery_agent()` — *not* P1's `deterministic_recovery_agent`.
Every campaign that does not explicitly pass `recovery_agent=` (`run_burden_plan_graph.py`,
`run_burden2`, `run_burden3`, `run_orbit`) therefore loses transient-retry coverage the moment
P1 and P5 both land. Only the Flow Editor runner escapes, because it explicitly passes the
composed agent from P6 (`run_flow_editor_streamline_plan_graph.py:1481–1484`, which tries
review-continuation first and falls back to `deterministic_recovery_agent`). **This remains the
single most important cross-patch finding in the set.** The composition belongs at the platform
default level, not per-runner.

**(b) `additional_cycles` does not accumulate.** `review_fix.py:584`:
`cycle_limit = (sensitive|mechanical)_cycle_limit + self.additional_cycles`, while
`cycle = self.resume_from_cycle` (line 586). The caller
(`feature_run.py:1053–1058`) passes `resume_from_cycle=review_fix_result.cycles` — the
predecessor's **cumulative** cycle count — but `additional_cycles=review_fix_policy.continuation_cycles`,
a **constant** (default 2, `review_fix.py:63`). A *second* consecutive continuation therefore
starts at `base + 2` against a limit of `base + 2`: it burns one review call and exits
immediately on `cycle_limit`. Currently masked only by the unchanged-strategy guard firing on
identical reason text. No test covers a second continuation. Not disclosed by the implementer.

**Merge recommendation: merge with changes.** Fix the accumulation (accumulate granted cycles
across continuations, or derive the limit from the ledger rather than recomputing from policy);
bind the *composed* agent (see P6) as the platform default so P1 is not regressed; add a
second-continuation test.

---

## P6 — `flow_editor_recovery_agent` composition + `recovery_limit` 3→6

**Commit:** `d16cf02`
**Files:** `experiments/run_flow_editor_streamline_plan_graph.py` (+56/−14),
`tests/test_flow_editor_recovery_agent.py` (new, +118)

**Classification: mixed.** Re-read in this pass: the function body
(`run_flow_editor_streamline_plan_graph.py:1478–1484`) is a two-policy composition over
platform types with zero campaign coupling — try review-continuation, else deterministic.
Only its name and location are campaign-specific.

**Evidence.** 118 lines of test, but the test `importlib`-loads the ~1600-line experiment
script to reach the function, and its final assertion greps the runner's source text.

**Merge recommendation: merge the function, not the file.** Relocate the composition into
`harness_labs/featurerun/feature_run_policy.py` (e.g. `standard_composed_recovery_agent()`) and
make it the platform default binding in P5's `feature_run.py:1399` block. Leave
`recovery_limit=6` in the runner as campaign configuration.

---

## P7 — `experiments/few_autoresume.py` operator driver

**Commits:** `3f3c335`, `c5ffb74`, `dde6434`
**Files:** `experiments/few_autoresume.py`

**Classification: run-specific.** Hardcoded runner path, `pgrep` pattern, run root, receipt
path, and attempt-id regex. No tests.

**Merge recommendation: do not merge as written.** Three of its ideas are generic operator-loop
primitives and are worth a parameterized follow-up (`scripts/plan_graph_autoresume.py`):
quiescence-wait before relaunch; frontier derived from `plan_node_failed` events; a no-progress
guard that stops after three identical escalations. Note P11 changes the contents of the
frontier this driver consumes (see Interactions).

---

## P8 — Campaign-runner configuration changes

**Commits:** `150d91d`, `8bd5532`, `56aad94`, `f08ff36`, `4a6f296`, `4c893aa`
**Files:** `experiments/run_flow_editor_streamline_plan_graph.py` only

**Classification: run-specific.** Preflight-failure tolerance in the verify stage; coordinator
spec default with a `FEW_COORDINATOR_SPEC` env override; coordinator timeout 900s→7200s;
`verification_repair_limit` 3→6; dirty-baseline grant wiring.

**Merge recommendation: do not merge.** Two observations deserve a narrow platform follow-up:
`require_preflight_success=False` for the fix loop's verify stage (with the node gate staying
strict) reads like a defensible platform default; and a 900s coordinator-session timeout is
structurally wrong for any campaign whose workers run longer than fifteen minutes — that is a
platform-default bug wearing a runner-config costume, and `56aad94` / `f08ff36` are the receipt.

---

## P9 — Dashboard response cap 1 MiB → 8 MiB

**Commit:** `a1c5069` (branch `dashboard_improve`, **not** on the campaign branch)
**Files:** `harness_labs/observability/dashboard_server.py` (+6/−1),
`tests/test_dashboard_api.py` (+3/−1)

**Classification: generalizable.** `/api/catalog` crossed 1 MiB at roughly 25 graph attempts;
the dashboard then served a stale snapshot for about 10.5 hours while `/api/health` kept
reporting `ok`.

**Evidence.** No test reproduces the wedge itself; the existing cap test was updated to use the
constant.

**Merge recommendation: merge now.** Trivially small and unblocks observability. Name the real
follow-up separately and give it priority: **the health check must detect a frozen-but-serving
snapshot, not merely that a snapshot exists.** That is the more valuable patch and it does not
exist yet.

**Merge mechanics:** `git merge-tree` of `dashboard_improve` against the campaign tip conflicts
in `.claude/launch.json` only — configuration, not code. The two `harness_labs` changes merge
clean.

---

## P10 — `feature/plan-graph-attempt-carryover`

**Commits:** `ba9e781`, `5b260d0`, `c4550ba` (branch head `c4550ba`; unmerged)
**Files:** `harness_labs/plangraph/attempt_carryover.py` (new; 541 + 356 + 299 = ~1196 lines),
`harness_labs/plangraph/plan_graph.py` (+61, +13), `harness_labs/plangraph/plan_graph_audit.py`
(+23), `tests/test_plan_graph_attempt_carryover.py` (474 + 212 + 272 = ~958 lines)

**Classification: generalizable.** No campaign content.

**Evidence.** 43 test methods (re-counted at `c4550ba`) exercising real git/worktree operations
with the `FeatureRun` child faked. **Seven of those are `skipTest` stubs** in one class
(`"covered by AttemptCarryoverGraphTests"`, lines 928–946), inflating the apparent count. The
progress heuristic is weak: first-vs-last failing-test count only, with no monotonicity or rate
requirement.

**Blocking gap (re-confirmed).** `carryover_context_for_attempt` (`attempt_carryover.py:1012`)
exists and is exported, but **nothing on the branch calls it** — a `git grep` across the branch
finds only its definition and its `__all__` entry. The feature's own named danger, in the
author's words in `5b260d0` — "a cycle-1 'implement from scratch' prompt against a non-blank
tree" — therefore has a query-layer fix with no production caller. Default-off via
`HARNESS_LABS_PLAN_GRAPH_ATTEMPT_CARRYOVER` (`attempt_carryover.py:58`), read at launch time
(process-wide, not per-constructor). Never run on a live campaign.

**Merge recommendation: hold.** It is safe to merge default-off purely for review convenience,
but only if explicitly labelled *not yet activatable*. Do not merge expecting it to be usable:
the documented main danger is unmitigated.

---

## P11 — `feature/plan-graph-continue-after-block`

**Commit:** `c4007e0` (unmerged)
**Files:** `harness_labs/plangraph/plan_graph.py` (+121/−14),
`tests/test_plan_graph_parallel_run.py` (+142),
`docs/development/plan-graph-sibling-independent-node-relaunch.md` (new, 263 lines)

**Classification: generalizable.** A scheduler "withheld" set plus a
`continue_independent_after_block` constructor flag (`plan_graph.py:1156`, default `False`,
validated as a bool at 1206), so a ready independent node is not starved when an unrelated
sibling blocks. The author correctly declined to build full mid-flight resume and flagged that
as needing a human ADR decision.

**Evidence.** Good tests, including one that pins the *default* (off) rule and one asserting a
real `escalation.json` off disk. One `Timer(1.0)` / `wait(timeout=20)` test carries mild flake
risk. Never run live; the author recommends default-off until validated.

**One ungated behaviour change (re-verified, still ungated).** The widened `retry_frontier` —
`plan_graph.py` diff lines 235–250, `retry_frontier = [failed_run_id] + sorted(every other
failed/blocked node)` — applies on **every** blocked attempt regardless of the feature flag.
Any consumer of `escalation.json` is affected, including P7's driver. The author disclosed this
as an open question; it is not a hidden defect, but it is a decision that has not been made.

**Doc reference check (new in this pass).** The design doc cites
`experiments/few_autoresume.py` (line 24) and `run_flow_editor_streamline_plan_graph.py`
(line 25). Both exist on the campaign branch (they are P7 and P8) but **neither exists on
`main`**. If P11 merges to `main` without P7/P8, the doc's references dangle.

**Merge recommendation: merge with changes.** Gate the frontier widening behind its own flag,
or make it an explicit separately-reviewed decision with P7 updated in the same change; fix the
two dangling doc references; then merge with `continue_independent_after_block` default-off.

---

## P12 — Join-conflict resolution store *(new since the prior audit)*

**Commit:** `deb8c73` — *"Join-conflict resolution store: verified operator channel for
PlanGraph sibling-join conflicts"*, authored 2026-08-17 23:56:53 −0400 on
`claude/join-conflict-resolution-store`, now the tip of
`claude/retinology-flow-editor-graphrun-5dd0de` (both refs point at the same commit).

**Files:** `harness_labs/plangraph/plan_graph_join.py` (new, 589 lines),
`harness_labs/plangraph/plan_graph.py` (+111/−7), `tests/test_plan_graph_join.py` (new, 559 lines).

### What it does

`PlanGraph._join_candidates` performs the controller-owned mechanical merge between sealed
sibling candidates. Its long-standing rule is that a real conflict there is a **plan defect** —
the siblings' `allowed_paths` were not disjoint in effect — and the join step must never invent
a resolution. That rule is unchanged. What this patch adds is a narrow, auditable channel by
which an operator who has *actually diagnosed* the conflict can hand a verified resolution back
to the mechanical step:

1. **`describe_join_conflict(repository, label, parent_a, parent_b)`** reproduces the conflict
   with `git merge-tree --write-tree` and returns a structured description: both parents, both
   parent trees, all merge-base trees, the conflicted paths and their stages, the marker-laden
   file content (capped at 64 KiB per file), the raw merge-tree output, and a content-derived
   `resolution_key`. It **raises on a clean merge** — you cannot describe, and therefore cannot
   register a resolution for, a pair that does not conflict.
2. **`JoinConflictResolutionStore`** is an append-only, `flock`-serialized, `fsync`-ed JSONL
   journal per plan lineage at `<run_root>/.plan-graph-join-resolutions/<lineage_id>.jsonl`,
   following the `RetryBudgetLedger` durability shape. Sequencing is explicit and monotonic;
   the fold rejects any out-of-order or malformed line as journal corruption rather than
   skipping it.
3. **`PlanGraph._resolve_join_conflict`** (`plan_graph.py:1850–1934`) consults the store on
   conflict. With a valid registration it commits the registered tree with both parents exactly
   as a clean merge would have, tagging the message
   `PlanGraph join <label> (<graph_run_id>) [conflict resolved: <key12> seq <n>]`. Without one
   it writes a durable JSON conflict artifact under `<run_root>/.plan-graph-join-conflicts/`
   (carrying the full description plus a ready-to-run registration argv) and raises with the
   full parent ids and conflicted paths.

**The keying design is the interesting part.** `_resolution_key` hashes
`{protocol, label, parent_trees, sorted(merge_base_trees)}` — *tree* ids, not commit ids —
because synthetic intermediate join commits are re-created per attempt with fresh timestamps
and therefore have unstable commit ids, while the merge *inputs* are stable. Observed commit
ids are still journaled as provenance. This is not a theoretical nicety; see the live evidence
below, where it is precisely what made resolutions 2 and 3 survive.

**Registration fail-closes**, and I re-read every check (`_verify_resolved_tree`, lines 304–350):

- the pair must *really* conflict right now (registration calls `describe_join_conflict`);
- `resolved_tree` must exist as a tree object;
- it may differ from the mechanical automerge tree **only at the conflicted paths** — every
  auto-merged path is preserved bit-for-bit, so an unrelated tree cannot be smuggled through;
- it must change at least one conflicted path (an unchanged tree is by definition still
  marker-laden);
- no conflicted path may retain conflict markers;
- re-registration for the same key is idempotent if the tree matches, and otherwise requires an
  explicit `supersede=True`.

`resolve()` (used by the join) does not trust the journal: it re-derives the conflict, checks
the live `resolution_key` still matches the record, and re-runs the full tree verification.
A registration that no longer verifies **raises loudly** rather than being silently ignored.
Sealed candidate history is never rewritten; the resolution lives only in the synthetic join
commit's tree. Resolved trees are anchored against gc via
`refs/plan-graph-join/<lineage>/<key16>` — I confirmed all three such refs exist in the
Retinology repository.

An operator CLI (`python3 -m harness_labs.plangraph.plan_graph_join`, subcommands
`describe` / `register` / `list`) rounds it out.

### Classification: generalizable

I read all 589 lines of `plan_graph_join.py` looking for campaign coupling and found **none**.
There are no Retinology paths, no WP node ids, no Flow-Editor vocabulary, no assumptions about
what the conflicting content is. The module's only inputs are a repository path, a run root, a
lineage id, a join label, and two commit-ish names. The `plan_graph.py` integration is likewise
generic.

**The one campaign coupling is in the test file, not the code.** `tests/test_plan_graph_join.py`
carries a final `RetinologyWp25EndToEndTest` class pinned to
`RETINOLOGY_REPO = os.environ.get("RETINOLOGY_REPO", "/Users/kirillzaslavsky/claudeprojects/Retinology")`
and two hardcoded WP-25 parent SHAs. It is `skipUnless`-guarded on fixture availability, so on
any other machine it **silently skips**. That is the right shape for a fixture-dependent test,
but it means the strongest test in the file contributes nothing to CI on `main`. It is also, in
fairness, an honest artifact: it is a regression test against a real conflict that really
happened.

### Test evidence

Sixteen test methods across four classes, all against real git repositories built in
`tempfile.TemporaryDirectory` — no mocked git anywhere:

- `DescribeJoinConflictTest` (3): full-detail description; refusal on a clean pair; and — the
  design-critical one — `test_key_is_stable_across_commit_identity_changes`, which pins the
  tree-based keying.
- `JoinConflictResolutionStoreTest` (7): register/lookup round trip; lookup misses for a
  different label or pair; and five distinct rejection paths — no real conflict, a tree touching
  unconflicted paths, an unrelated tree, the unmodified automerge tree (markers retained), a
  missing tree object — plus supersede semantics.
- `JoinCandidatesResolutionTest` (3): the unresolved-conflict raise **including reading the
  written artifact back off disk** and asserting its contents; the registered-resolution success
  path; and a resolution registered under a *different label* correctly failing to apply.
- `RetinologyWp25EndToEndTest` (1): described below.

**The end-to-end test is real, not synthetic.** It `--mirror`-clones the actual Retinology
repository into a temp directory (the live clone is never written to), reproduces the real
WP-21 × WP-22 conflict from the two sealed candidate commits, asserts the exact three conflicted
paths, *rebuilds the operator's resolution programmatically* from the marker blocks (WP-22's
colour token, WP-21's import superset, a union of the two parity-oracle coverage obligations),
registers it, runs the production `_join_candidates`, and then asserts the resulting commit's
tree equals the registered tree, its two parents are exactly the two sealed candidates, and no
resolved file retains markers. This is the only end-to-end test against real content anywhere
in this audit set.

**Test caveats.** `minimal_plan_graph` builds the graph with `PlanGraph.__new__` and sets five
attributes by hand, deliberately bypassing `__init__`. This keeps the tests on the production
`_join_candidates` code path (which is the point) but means the `__init__` wiring that
constructs `self.join_resolutions` (`plan_graph.py:1182–1184`) is **not covered by any test** —
it is covered only by the live evidence below. There is also no test of the CLI entry point, and
none of concurrent registration despite the advisory locking.

### Live evidence — this is the part that distinguishes P12

**This mechanism has been used three times, in production, on the live campaign, to unblock
WP-25 — the campaign's final node.** I verified this independently end to end; the claim holds
in every particular I checked.

**The journal.** `logs/runs/few-graph/.plan-graph-join-resolutions/lineage-d96466ab…a81a.jsonl`
contains exactly three `registered` events, sequences 1–3, all `label: "WP-25"`,
`actor: "operator"`, `supersedes: null`, protocol `join-conflict-resolution-store/1`:

| seq | conflicted paths | resolution key (12) | resolved tree (7) |
|---|---|---|---|
| 1 | `flow_editor.css`, `flow/canvas.js`, `tests/test_flow_parity_oracle.py` | `3586a296407e` | `2681dcf` |
| 2 | `tests/test_l2_flow_editor.py` | `12ad4cacebd5` | `cd235b6` |
| 3 | `l2_pipelines.py`, `flow_editor.css`, `canvas.js`, `test_flow_parity_oracle.py`, `test_l2_batch.py`, `test_l2_dark_mode.py` | `4a301ca892fb` | `07f8934` |

The `reason` fields are substantive engineering prose, not boilerplate — seq 2's, for instance,
records that WP-21 had hardcoded `--single-process` as a Chromium stability fix while WP-23 added
a `launch_args` parameter and the merge dropped WP-21's default, and that the resolution
defaults to `--single-process` when `launch_args is None` and honours a caller-supplied value
otherwise. I checked the resolved blob: `tests/test_l2_flow_editor.py` at tree `cd235b6` reads
`args=launch_args if launch_args is not None else ["--single-process"]` (line 90), with
`launch_args=None` in the signature (line 78). The registered reason and the registered content
agree.

**The join commits.** In `/Users/kirillzaslavsky/claudeprojects/Retinology`:

```
99886bc PlanGraph join WP-25 (flow-editor-uistreamline-attempt-84) [conflict resolved: 4a301ca892fb seq 3]
da9d2c4 PlanGraph join WP-25 (flow-editor-uistreamline-attempt-84) [conflict resolved: 12ad4cacebd5 seq 2]
684daf8 PlanGraph join WP-25 (flow-editor-uistreamline-attempt-84) [conflict resolved: 3586a296407e seq 1]
```

All three are genuine two-parent merge commits dated 2026-08-18 00:09:53 −0400, forming a chain
(`684daf8` → parent of `da9d2c4` → parent of `99886bc`) as WP-25 joins its five predecessors
pairwise. **Each commit's tree is byte-identical to the corresponding journaled
`resolved_tree`** (`2681dcf`, `cd235b6`, `07f8934` respectively) — verified with
`git log -1 --format=%T`. `684daf8`'s parents are exactly seq 1's journaled parents
(`9588507`, `774cbe7`).

**The tree-keying design demonstrably paid for itself in production.** Seq 2's journaled
`parents[0]` is `0524f3f`, but the live join's first parent is `684daf8`. Seq 3's journaled
`parents[0]` is `54c3de1`, but the live join's is `da9d2c4`. Those are *different commits* —
they are earlier incarnations of the synthetic intermediate join, re-created with fresh
timestamps on a later attempt. The resolutions applied anyway because the key is derived from
trees: seq 2's `parent_trees[0]` is `2681dcf`, which is exactly `684daf8`'s tree; seq 3's is
`cd235b6`, exactly `da9d2c4`'s tree. The specific hazard the module's docstring warns about
occurred in production and the design absorbed it.

**It went through the real code path, and through the intended operator loop.** Two conflict
artifacts sit on disk in `logs/runs/few-graph/.plan-graph-join-conflicts/` — `…-12ad4cacebd5…`
(72 KB, written 00:00) and `…-4a301ca892fb…` (380 KB, written 00:03). These are only ever
written by `_resolve_join_conflict` when *no* resolution is registered. The timeline they
establish is exactly the loop the patch was designed for: seq 1 registered (journal directory
created 00:00) → resume → join 1 clears, join 2 conflicts and writes its artifact (00:00) →
operator diagnoses from the artifact and registers seq 2 → resume → join 3 conflicts, artifact
written (00:03) → seq 3 registered (journal mtime 00:09) → attempt-84 launches 00:09:52 and all
three joins succeed at 00:09:53. `logs/runs/few-graph/flow-editor-uistreamline-attempt-84/events.jsonl`
shows the controller reusing 28 sealed nodes and then emitting `plan_node_started` at
04:09:53Z — WP-25 is running against the joined base. There is no evidence of any bypass, and
none is even structurally available: the message suffix `[conflict resolved: … seq …]` is
produced *only* by `_resolve_join_conflict`, so its presence in all three commits is proof the
resolution path executed inside `_join_candidates`.

**All three registrations post-date the merge into the campaign branch** (`deb8c73`, 23:56:53
2026-08-17; first artifact 00:00 2026-08-18), so the mechanism was exercised as merged, not as
a prototype.

**Independent content spot-check (one file per resolution).**

- *seq 1* — `flow_editor.css` at the resolved tree `2681dcf` reads `color: var(--ink)` at the
  chip rule, i.e. WP-22's token, exactly as the reason claims ("pick WP-22's `--ink`,
  later-reviewed value"); the diff against WP-21's parent tree shows precisely that one-line
  colour change plus WP-22's additive blocks.
- *seq 2* — the `launch_args` / `--single-process` semantic merge described above.
- *seq 3* — `git diff --name-only <automerge_tree> <resolved_tree>` returns exactly the six
  journaled conflicted paths and nothing else, and all six carry zero `<<<<<<<` markers.

For every one of the three, the resolved tree differs from the mechanical automerge tree **only
at the journaled conflicted paths** — which is the invariant `_verify_resolved_tree` enforces,
confirmed here after the fact against the real trees rather than only against the code.

### Assessment and merge recommendation

**Merge now — and it is the strongest-evidenced patch in the entire set.** Every other patch
audited here rests on unit tests (P1, P2, P5, P10, P11), on a single incident report (P3, P9),
or on tests that do not exercise the path they claim to (P4). P12 is the only one with genuine
production validation: it ran three times, on real conflicts, in the real controller, on the
real repository, and the artifacts it produced are independently verifiable against the git
object store — which is exactly what I did. It is also, on reading, the most carefully
constructed: it preserves rather than weakens the "a sibling conflict is a plan defect"
invariant, it fail-closes on every registration path, it re-verifies at use time instead of
trusting its own journal, and it made a non-obvious design call (content keying) that a
production hazard then vindicated within hours.

Two things worth doing, neither blocking:

1. **Add a test that exercises the `__init__` wiring.** `self.join_resolutions` is constructed
   at `plan_graph.py:1182`; every test bypasses `__init__`. Live use covers it, but `main`
   should not depend on that.
2. **Decide the fate of `RetinologyWp25EndToEndTest` before merge.** As written it silently
   skips off this machine. Either keep it with the skip made loud (it is a valuable regression
   fixture), or extract its resolution-shape assertions into a synthetic repository so CI on
   `main` gets the coverage.

Neither should hold the merge. One smaller note for the reviewer: the conflict artifacts are
uncapped in size (380 KB for the six-path conflict, since marker-laden file content is embedded
at up to 64 KiB per path) and are never pruned. Harmless at this scale, worth a follow-up if
conflicts become routine — though if conflicts become routine, the plan's `allowed_paths` are
the real bug.

---

## Interactions

**P1 × P5 — still the most important finding.** Re-verified at `deb8c73`: P5's
`feature_run.py:1399–1411` binds `standard_review_continuation_recovery_agent()` as the default
whenever `recovery_agent` is absent, silently displacing P1's `deterministic_recovery_agent`
for every PlanGraph campaign that does not pass one explicitly. `run_burden`, `run_burden2`,
`run_burden3`, and `run_orbit` all fall into that hole. **P1 and P5 must not land together
without P6's composition promoted to the platform default.** The clean landing is: P6's
composed agent moves into `feature_run_policy.py`, and that becomes the binding at
`feature_run.py:1399`.

**P4 × (P5 / P6).** P5 and P6 both increase the number of repair/review cycles a node runs. P4
attaches images per verification round, on by default, with no spend accounting and no audit
event. Image spend roughly doubles and remains entirely uninstrumented. Landing P4 before P5/P6
without instrumentation compounds an already-invisible cost.

**P5 × P10.** Overlapping intent (both are about not throwing away a failed attempt's work),
not conflicting. Worth a single design conversation rather than two independent merges.

**P10 × P11.** Re-checked in this pass with `git merge-tree`. Each merges **cleanly** against
the current campaign tip `deb8c73` individually — including against P12's `plan_graph.py`
changes — but they **conflict with each other** in `PlanGraph.__init__`
(`harness_labs/plangraph/plan_graph.py`). Mechanically resolvable; sequence them deliberately
and resolve once. Separately, P11's widened `retry_frontier` interacts with P10's
carryover-candidate gating in a way neither test suite covers.

**P11 × P7.** P11 changes `escalation.json`'s `retry_frontier` contents unconditionally,
regardless of its feature flag; P7's driver consumes that field. If P11 lands, P7's frontier
derivation changes meaning whether or not anyone opts in.

**P9 × campaign branch.** `dashboard_improve` conflicts with the campaign tip only in
`.claude/launch.json`. The `harness_labs` changes merge clean.

**P12 × everything else — no code-level interaction found.** P12 touches
`plan_graph.py` (`__init__` + `_join_candidates` + one new private method) and adds one new
module. P5 also touches `plan_graph.py` (+97) but in the review-continuation/ledger area, and
P5 is already merged into the campaign branch *below* P12, so that composition is already
resolved and tested by construction. P10 (+61/+13 in `plan_graph.py`) and P11 (+121/−14 in
`plan_graph.py`) both still merge clean against `deb8c73`, so P12 introduces no new merge
hazard for either. One conceptual adjacency worth naming, not a conflict: P12 is the
*curative* answer to sibling-path overlap and P1's overlap warnings are the *preventive* one —
they are complementary, and P12's three live conflicts are direct evidence that P1's warnings
were pointing at something real.

---

## Summary

**Ready to merge as-is (5)**

| | patch | why |
|---|---|---|
| 1 | **P2** — repair_selection fixpoint fix | highest value, lowest risk; was burning compute on every resume |
| 2 | **P12** — join-conflict resolution store | only patch with genuine production validation (3 live resolutions, independently verified) |
| 3 | **P1** — deterministic default agent + overlap warnings | strong tests; corroborated after the fact by P12's real conflicts |
| 4 | **P3** — DNS/websocket signatures | 7 lines, incident-justified; merge with P1 |
| 5 | **P9** — dashboard 8 MiB cap | trivial; unblocks observability |

**Needs work before merge (4)** — P5 (fix `additional_cycles` accumulation; bind the composed
agent as the platform default; add a second-continuation test) · P6 (relocate the composition
into `feature_run_policy.py`; leave the runner config behind) · P4 (fix the vacuous scoping
test; add a Codex-argv test; reconsider on-by-default; verify or drop the Claude path) ·
P11 (gate or explicitly decide the unconditional frontier widening; fix two dangling doc
references; merge default-off)

**Hold (1)** — P10: mergeable default-off for review convenience only, and only if labelled
not-yet-activatable; its main documented danger has no production caller.

**Do not merge, campaign-specific (2)** — P7 (extract the three operator-loop primitives into a
parameterized `scripts/plan_graph_autoresume.py` instead) · P8 (but file two follow-ups: the
fix-loop preflight-tolerance default, and the 900s coordinator-timeout default).

### Suggested incremental merge order

1. **P2** — independent, immediate payoff, no interactions.
2. **P12** — independent of everything else, best-evidenced, and it unblocks the campaign's own
   final node. Add the `__init__`-wiring test and settle the Retinology fixture test first.
3. **P9** — independent (resolve the `.claude/launch.json` conflict, which is config only).
   File the frozen-snapshot health-check follow-up at the same time.
4. **P1 + P3 together** — but *only* after P5/P6's default binding is settled, or the
   deterministic agent will be dead code for PlanGraph campaigns the moment P5 lands.
5. **P6-relocated + P5-fixed, as one change** — the composed agent must become the platform
   default in the same commit that introduces the continuation default, so P1 is never
   transiently regressed.
6. **P4** — after its test defects are fixed, and preferably with an audit event so the spend
   P5/P6 amplify is at least visible.
7. **P11** — after its frontier decision is made and P7's replacement exists to consume it.
8. **P10** — last, or not yet; it needs a production caller and a stronger heuristic before it
   is worth activating.
