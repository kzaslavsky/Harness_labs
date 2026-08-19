# Session handoff — 2026-08-18/19 platform-patch campaign

Status: active. `main` at `ce5dc3a`, pushed. One reviewed-but-unmerged commit
and three unmerged documents are listed below.

Date: 2026-08-19

## What this document is

A handoff after a long session that merged fourteen changes to `main`, and a
re-ranked list of what is left. Written because session context is high and
the next stretch of work should start clean.

Everything below was verified in-session unless explicitly marked as reported
by a subagent and not independently checked.

---

## Where `main` is

`ce5dc3a`. Full suite: **658 passed, 2 skipped, 1 xfailed.** The single xfail
is deliberate and must stay — see "the SEAM" below.

Two operational notes for whoever picks this up:

- **`origin/main` has other contributors.** A push was rejected mid-session
  because two commits (a doc deletion and a README trim) arrived from
  elsewhere. They were merged, not forced over. Fetch before assuming.
- **`tests/test_dashboard_e2e.py` is timing-sensitive** and flaked once under
  load. Re-run it in isolation before concluding a change broke it.

### Merged this session, in order

| commit | what |
|---|---|
| `159136d` | Audit patches P1, P2, P3, P9, P12 |
| `28f548c` | Frozen-snapshot health detection + P11 (continue-after-block, default off) |
| `0ef7d1b` | P5 + P6: review continuation, composed recovery agent as platform default |
| `3c99707` | Per-class recovery budgets |
| `9eff246` | P4 verification images, scoping fixed, spend made visible |
| `8533221` | Parameterized autoresume driver (replaces P7) |
| `38ef294` | Decomposition refinement loop |
| `a8ed261` | Intent-aware narrowing |
| `00b4e79` | Advisory when grants exceed declared intents |
| `4c79877` | Scope-screening false-positive repair + adversarial suite |
| `bc4dd56` | `dashboard_improve` port survey (document) |
| `ce5dc3a` | Transfer routing + multi-node frontier recovery |

The governing document for the first group is
[`HARNESS_LABS_PATCH_AUDIT_20260818.md`](HARNESS_LABS_PATCH_AUDIT_20260818.md),
also on `main`.

### Still held from that audit

**P7 and P8** (campaign-specific; P7's ideas were reimplemented as
`scripts/plan_graph_autoresume.py`). **P10** attempt carryover — never run
live, `carryover_context_for_attempt` has no production caller, and it now
also needs rebasing against P11's `PlanGraph.__init__` changes.

---

## Unmerged artifacts

| branch | commit | what | disposition |
|---|---|---|---|
| `fix/campaign-preconditions` | `98f92e0` | Controller liveness lease (fix 3 of 3) | **Needs review — see item 1** |
| `worktree-agent-af9ce5232915de3ee` | `7a99356` | Scope-block study | Merge when convenient |
| `worktree-agent-aacc87babf81c7a02` | `4fe06cb` | Tech-debt-collector design (declines it) | Merge when convenient |
| `worktree-agent-aa29a4426ec30342a` | `83c955b` | Abandoned partial leak fix | **Do not merge.** Superseded by `4c79877` |

Fixes 1 and 2 from `fix/campaign-preconditions` are already on `main` via
`ce5dc3a`; only the third commit is outstanding.

---

## Re-ranked backlog

### 1. Review the controller-liveness commit (`98f92e0`) — decide, then merge or reject

**The operator's stated inclination is to reject it as overengineering.** That
inclination is recorded here as the starting position for the review, not as a
decision already taken.

The problem it addresses is real and was verified: `run_catalog.py` consumes a
`liveness.json` lease, and the only writer of that filename in the repository
is `scripts/dashboard_fixture_run.py`. So the run catalog reports
`liveness_unavailable` for every real non-terminal run, and the dashboard
cannot show whether anything is alive.

What the commit does, and why it warrants scrutiny: it adds a new
`harness_labs/core/controller_liveness.py`, changes `AuditJournal` to take an
opt-in `controller_kind` and to start and release a lease, wires three call
sites, and introduces **one shared daemon thread and an atexit sweep per
process**. A background thread in a core audit class is the largest single
risk in the batch, and it is the part to judge hardest.

Two supporting arguments, both worth checking rather than accepting:

- It claims `harness-controller-liveness/1` is not a new convention — that it
  has a schema and is specified in `live-plangraph-dashboard-plan.md` §3 down
  to the 0600 atomic write and the heartbeat, and was simply never
  implemented. If true, this is implementing an existing spec.
- It **removed the `_CHILD_LIVENESS_NAMES` alias** in `plan_graph_audit` and
  `plan_graph_autoresume`, arguing it is a trap rather than a hint: both
  readers refuse to look when more than one candidate filename is present, so
  once every FeatureRun writes `liveness.json`, a child that also wrote
  `plan-graph-liveness.json` becomes unobservable. This is a behaviour change
  to the autoresume driver's quiescence check and deserves its own look.

Reported by the subagent, verified only to the extent of running the suite:
671 passed with it, 658 without. Its dead-vs-live rule reuses the existing pid
plus `process_start_token` identity from `reclaim_orphaned_successor_attempt`.

**A cheaper alternative exists and should be weighed explicitly:** teaching
`run_catalog` to read the markers production already writes, rather than
having production write a second file. The subagent rejected this because
`plan-graph-admission-liveness.json` carries no run id, kind, or heartbeat —
check whether that objection is decisive or merely inconvenient.

If rejected, the underlying gap remains open and should re-enter this list.

### 2. Run a real campaign

**The dominant standing risk, and now also the instrument that answers several
open questions.** Roughly ten features on `main` have never run live: review
continuation, the composed recovery agent, per-class budgets,
continue-after-block, verification images, the autoresume driver, the
refinement loop, intent-aware narrowing, the grant advisory, and the
scope-screening repair. The tests behind them are unusually good — real pytest
runs, a real 26-node plan, real git repositories, an independent adversarial
suite — but they all get exercised together on the next campaign.

It is an instrument because the screening counters merged in `4c79877` are
exactly what the collector design document's Stage 1 says will settle the
question below: how much of the 45 silent screens was formatting artifact
versus genuine cross-node obligation, measured uncontaminated. Falsified if
screened counts stay flat.

### 3. Port `dashboard_improve` steps 1–3

Full analysis in
[`dashboard-improve-port-plan.md`](dashboard-improve-port-plan.md). Trial
merge conflicts in only three files, all additive; contract errors unchanged
at 29; 82 new passing tests.

Land: catalog display names and `block_escalation`; then `graph_metrics.py`
(which carries three already-fixed arithmetic bugs where post-join node rows
were inflated 1.0–6.6×); then the live metrics endpoint and its panels.

**Hazard to respect:** a file-level *copy* of `dashboard_server.py` or
`tests/test_dashboard_api.py` reverts the frozen-snapshot health check from
`f992748`. The second is worse — deleting the health tests leaves the suite
green having stopped asking. Merge, never copy.

**Held, needing an operator decision:** run-root self-registration writes
`~/.harness_labs/dashboard-audit-roots.json` on every graph run with **no
opt-out**. That is a policy call, not dashboard plumbing.

**Held, needing a commitment:** the snapshot viewer, until someone will run
the backfill. An empty Completed tab is worse than no tab.

### 4. `open_obligations` is unconditionally empty

`plan_graph.py:2593` reads `node.get("finding_obligations", [])`, but that
field is written onto the FeatureRun *request* (`plan_graph.py:645`), never
onto the node record. Found by a subagent that filed it separately rather than
smuggling it into an unrelated change; mechanism spot-checked, consequence not
measured.

### 5. The `origin_node` bypass skips later guards

In `ReviewLedger.ingest`, the `origin_node` branch `continue`s past *all*
later branches, so a transferred finding also skips the citation guard and the
sub-threshold `note` demotion. A low-score inherited finding therefore stays
`open` and can block forever. Over-blocking direction, so it loses no
findings — which is why the adversarial agent reported it and left it alone.

### 6. Stabilize `tests/test_dashboard_e2e.py`

Drives a real browser over CDP with wall-clock deadlines. Flaked once this
session.

### 7. Smaller items

- **Unchanged-strategy guard compares globally, not per class.** In
  `_recover_abnormal`; reachable only when a continuation immediately follows
  an identical continuation.
- **Whitespace-padded anchors.** A non-required `" feature.txt"` is still
  screened. The adversarial test that appears to cover this passes for the
  wrong reason — its fixture is `requires_disposition`, so the exemption
  rescues it. Decide whether to strip; POSIX permits the filename.
- **Severity for shared directory grants** stays `info` while same-file
  overlaps are `high`. Partly mitigated now that the refiner and advisory
  surface them.
- **`900` hardcoded** in `controller_live_scenarios.py:392,420`, alongside a
  hardcoded Retinology path. Scenario-module cleanup, not a platform default.
- **32 directory grant entries remain unroutable** after the routing fix,
  because `_transfer_targets_for` drops any grant held by two equidistant
  descendants and `tests` is granted to 19 of 26 nodes. Decomposition shape.
- **Redundant local branches** from this session can be pruned.

### 8. Deferred pending campaign evidence

- **The SEAM.** Marked in `ReviewLedger.ingest`. Genuinely out-of-grant
  findings are still discharged, but now counted. Whether they should block,
  carry to a successor, or route to a collector is open by design.
- **Bidirectional `finding_transfer_targets`.** Motivated by the finding that
  17 of 32 unique-owner cases needed a *sealed upstream* contract widened,
  which downstream-only transfer structurally cannot do. Ranked fourth of six
  by the design document, because a sealed upstream owner needs a repair
  successor, which changes its candidate commit and invalidates downstream
  joins.
- **Tech-debt collector: declined on evidence.** The refinement loop would
  deterministically strip its carried-debt grants as `narrow_grant` repairs
  and report the plan `clean`; its grants are unresolvable against digest
  revalidation at approval; and its fan-in would be 12–26 against the 5 that
  produced the campaign's worst node. Full argument in the design document on
  `worktree-agent-aacc87babf81c7a02`.

---

## Context a fresh session will need

**The SEAM.** In `ReviewLedger.ingest`, immediately above the anchor branch's
`record["outcome"] = "scope_screened"`. Everything reaching that line is a
finding the node genuinely cannot act on. Its behaviour is unchanged; only
counting was added. Block / carry / collect can be built there without
touching the false-positive repairs.

**The adversarial suite.** `tests/test_review_scope_guard_adversarial.py` was
written independently against pre-fix `main` and is the oracle for that area.
Seven of its eight expected failures became unexpected successes and had their
markers dropped. The one remaining `@unittest.expectedFailure` is the deferred
true-positive decision and must stay marked until that decision is made. Six
regression fences in it must keep passing: segment boundaries keep `src/app`
off `src/app_helpers`, a directory grant covers itself and its children, an
unanchored finding is never screened, a malformed grant raises loudly rather
than degrading to an empty grant, and a uniquely-bound transfer target still
receives its finding.

**Evidence documents on `main`:**
`HARNESS_LABS_PATCH_AUDIT_20260818.md`,
`plangraph-node-sizing-review.md`,
`dashboard-improve-port-plan.md`,
`plan-graph-sibling-independent-node-relaunch.md`.

**Working conventions that earned their keep this session:**

- Every fix proves its tests by reverting the production change, confirming
  the exact failing assertion, and restoring. This caught more than one test
  that would otherwise have passed for the wrong reason.
- An independent adversary, branched off pre-fix `main` and forbidden from
  editing the fixer's files, found four defects beyond the two it was briefed
  on. Worth repeating for anything critical.
- Verify subagent claims before relaying them. Several reports were accurate
  in substance but wrong in a detail that mattered — one described a fix as
  backward-compatible when it raised a spend ceiling, another blamed the
  wrong side of a mismatch.
- Test and push as separate steps. `pytest ... | tail && git push` gates the
  push on `tail`, not on the tests.
