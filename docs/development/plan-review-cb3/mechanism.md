# CB-3 plan review — MECHANISM lens

Subject: `docs/development/CONTRACT_BURDEN_RELAXATION_3_PLAN.md` (CB3-01..CB3-07).
Tree: `contract-burden-relaxation` @ `035c1f3`; RED_BASE `b49c194` (`git diff b49c194 HEAD` = plan doc only, so every line cited below is the RED_BASE line).
Base health confirmed: `python3 -m pytest tests/ -q` → **445 passed, 1 skipped in 61.22s**.

Method: read the named implementations, then check each AC's mechanism against them, then check the
live CB-2 journals under `logs/runs/cb2-graph/` for the specimen each red phase claims to reproduce.

---

## M1 (CB3-01, blocking) — rejected-dispatch provenance is *already landed* at RED_BASE; the specified red phase cannot go red

CB3-01's objective is "record every kernel-refused task command as a referenceable provenance
entry … so a coordinator … can cite the refusal itself instead of dying on `unknown provenance
reference`". That feature exists at RED_BASE:

- `harness_labs/controller_kernel.py:178` — state key `"rejected_task_dispatch_refs": []`
- `harness_labs/controller_kernel.py:236` — resume-path `setdefault`
- `harness_labs/controller_kernel.py:1258-1262` — on `_reject` with `code == "invalid_command"` and
  `command.type == "task.dispatch"`, appends `f"command:{command.command_id}"` and durably
  re-checkpoints via `audit.merge_checkpoint`
- `harness_labs/controller_kernel.py:555-559` — the provenance loop accepts such a ref *before*
  raising `unknown provenance reference`
- `tests/test_relax_kernel.py:480-536` — existing green coverage, including the negative case
  ("a rejection that never reaches task.dispatch evaluation must not mint a citable ref")

AC-CB301-4 specifies the red phase as "a retry citing a rejected dispatch's command id raises
`unknown provenance reference`". Against RED_BASE that test **passes**, so it is not a red phase; it
is a duplicate of `tests/test_relax_kernel.py`. AC-CB301-3's "only referenceability is added" is
already true — there is nothing left to add on that axis.

## M2 (CB3-01, blocking) — the live specimen is an unreferencable *task*, not an unreferencable rejected dispatch

Every `unknown provenance reference` in the CB-2 corpus (`grep -ho` over
`logs/runs/{cb-graph,cb2-graph}/*/events.jsonl`), 9 distinct occurrences:

| ref the coordinator cited | shape |
| --- | --- |
| `task:impl-cb208-r2`, `task:impl-cb208`, `task:impl-cb206`, `task:impl-cb202-repair` | task id |
| `decision:DEC-CB04-r2-placeholder` | decision id |
| `system-result:impl-cb205:5` | tool-result id |
| `command:impl-cb208`, `command:impl-cb205`, `command:system-result:impl-cb205:5` | *guessed* `command:` ref |

The plan's own headline specimen —
`logs/runs/cb2-graph/contract-burden-relaxation-2-attempt-3-CB2-08/events.jsonl` seq 124 — is:

```
"error_code": "unknown_evidence",
"message": "unknown provenance reference: task:impl-cb208-r2"
```

i.e. the coordinator cited a **task**, and the kernel only accepts evidence-catalog refs
(`self.evidence.contains(ref)`) or rejected-dispatch refs. Tasks, decisions and tool-result ids are
all in kernel state and all uncitable. CB3-01 as written fixes the one case that is already fixed
and leaves all four live cases broken.

The last three rows are the second half of the defect: the coordinator *tried* to use the landed
feature and guessed the ref wrong. The minted ref is `command:{command.command_id}`, where
command_id is `"c89f1136-…/toolu_01JZ…"` — a session-uuid/tool-use-id pair the coordinator cannot
reconstruct from the task id. The rejection receipt does carry `command_id`, but nothing tells the
coordinator that `command:<that>` is now citable. **Discoverability, not referenceability, is the
residual item-6 defect.**

Recommended re-scope for CB3-01: (a) accept `task:<id>`, `decision:<id>` and existing rejected-
dispatch refs uniformly in `_validate_envelope` (controller_kernel.py:555-559) by resolving against
`self._state["tasks"]` / `["decisions"]`; (b) have `_reject` put the citable ref into the receipt
(`CommandReceipt.effect_refs` or the message) so the coordinator is told what to cite. Both are
inside the node's grant.

## M3 (CB3-01, blocking) — "no retry-budget charge" has no referent in the kernel, and the supersede state machine cannot admit a rejection record

Two concrete impossibilities in AC-CB301-2:

1. **No retry-budget ledger exists in `controller_kernel.py`.** `retry.request` /`replan.request` route
   to `_recovery_request` (controller_kernel.py:980-989), which returns an anomaly event; `_apply`
   (controller_kernel.py:1356-1359) appends it to `self._state["anomalies"]`. No counter is
   incremented, nothing is charged. Retry budgets live elsewhere and outside CB3-01's grant:
   `harness_labs/plan_graph_budget.py:285` (per-finding counter) and
   `harness_labs/feature_run.py:1741,1794,2092,2139` (`env_retries`). So "no retry-budget charge is
   spent" is either vacuous (nothing to spend) or requires editing files CB3-01 does not own.
2. **`supersedes_task_id` cannot take a rejection record.** controller_kernel.py:820-853 requires
   the predecessor to be in `self._state["tasks"]`, to have `status == "failed"`, and then compares
   seven frozen-authority fields (`role`, `details_schema`, `acceptance_criteria`, `dependencies`,
   `parent_task_id`, `optional`, `may_delegate`) plus a `required_capabilities` subset check. A
   rejected dispatch creates **no task**, and AC-CB301-1 specifies the rejection record carries only
   "the refused command's payload digest" — a digest cannot be compared field-by-field. Either the
   record must store the full refused task payload (contradicting AC-CB301-1's digest wording), or
   the frozen-authority check must be skipped for rejection predecessors, which weakens the
   supersede state machine. The plan does not say which, and the second is keep-list-adjacent.

## M4 (CB3-01, design hazard) — journaling the rejection as a *kernel event* breaks optimistic concurrency

AC-CB301-1 wants the rejection "appended through the existing hash-chained journal, not a side
channel". It already is: `_reject` calls `self.audit.append("command_rejected", …)`
(controller_kernel.py:1229-1246) into the hash-chained audit journal (`event_hash`/`previous_hash`
visible in every `events.jsonl`). If the intent is instead a `KernelEvent`, note that `_commit`
(controller_kernel.py:1197-1227) bumps `self._state["revision"]` for every event batch, and
`_validate_envelope` (controller_kernel.py:534-535) rejects any command whose `expected_revision` is
stale. A rejection that bumps revision would invalidate the coordinator's *next* command — turning
one rejection into two. Any CB3-01 implementation must journal rejections without a revision bump.

## M5 (CB3-02, blocking) — the divergence hypothesis is right in kind, wrong in location; the CB2-03 specimen is unreproducible inside CB3-02's grant

The item-20 hypothesis is real. The controller-side selector checks **path coverage only**:

- `harness_labs/agent_mixture.py:333-335` — `receipted = set(receipt.get("changed_paths", ()))`;
  `if not dirty <= receipted: continue`. No `files`/content comparison anywhere in
  `_best_covering_receipt` (agent_mixture.py:310-342).

The executor-side preflight checks **path coverage AND per-file content state**:

- `harness_labs/claude_task_executor.py:501-506` and the byte-identical
  `harness_labs/controller_live.py:589-594`:
  `covered = bool(receipted_paths) and set(dirty_paths) <= receipted_paths`, then
  `covered = all(dirty_files.get(path) == receipted_files.get(path) for path in dirty_paths)`.

So a grant can be journaled `granted` on paths and then refused on content. That is exactly the
CB2-03 shape. **But the granter in the CB2-03 specimen is not `_controller_dirty_baseline_grant`.**
In `logs/runs/cb2-graph/contract-burden-relaxation-2-attempt-1-CB2-03/events.jsonl` seq 145 and 155,
the `dirty_baseline_adoption_grant_supplied | granted` events are emitted by
`_attach_dirty_baseline_grant` (`harness_labs/feature_run.py:1564-1601`), whose selector is a
**fourth** copy, `_dirty_baseline_receipt_ref` (`harness_labs/feature_run.py:1528-1561`) — also
path-only. Seq 156 then records the refusal.

`harness_labs/feature_run.py` is **not in CB3-02's owned paths**. As written, CB3-02 unifies three
of four sites and leaves the one that produced the specimen untouched; AC-CB302-4's red phase
("reproducing the CB2-03 attempt-1 specimen") is not constructible within the grant. Either add
`harness_labs/feature_run.py` + `tests/test_feature_run.py` to CB3-02's owned paths, or restate
AC-CB302-4 against the `agent_mixture` path and drop the CB2-03 citation.

Two further mechanism notes:

- `claude_task_executor.py:466-509` and `controller_live.py:554-597` are **byte-identical**
  duplicates (docstring included). That half of the unification is trivially safe.
- The controller-side selector runs `workspace_snapshot(repository)` at grant time
  (agent_mixture.py:301, feature_run.py:1586) and the executor re-snapshots at preflight
  (claude_task_executor.py:165, controller_live.py:259-266). Coverage is a function of *(receipt,
  workspace)*, not of the receipt alone. AC-CB302-1's justification — "the same decision … because
  both run the same code over the same receipt" — is therefore false in the general case: same code,
  different workspace input, different verdict. Seq 145 and 155 in the CB2-03 journal name the *same*
  `receipt_ref` across two review cycles while the tree changed in between, which is precisely the
  time-of-check/time-of-use shape. The AC must be restated as "the same code over the same receipt
  *and the workspace state observed at that moment*", and the plan should decide whether the grant
  carries a workspace fingerprint so a stale grant is detectably stale rather than silently refused.

## M6 (CB3-02/CB3-03, finding) — `allow_dirty_baseline` on the executors is vestigial, and `feature_run` bypasses role eligibility

`allow_dirty_baseline` is declared at `controller_live.py:176` and `claude_task_executor.py:78` and
is read **only** by the constructor validation at `controller_live.py:198-199` /
`claude_task_executor.py:100-101`. Neither `_resolve_dirty_baseline_grant` consults it — the
preflight gate is entirely `dirty_baseline_grant`. Separately,
`_attach_dirty_baseline_grant` (feature_run.py:1580-1581) gates only on
`hasattr(executor, "dirty_baseline_grant")`, never on role eligibility. AC-CB303-3 asserts
"role-level `allow_dirty_baseline` eligibility is still required — a role without eligibility gets
no grant regardless of receipts". That assertion is **false today on the feature_run path**, and
CB3-03 does not own `feature_run.py` either. Make it true or drop the claim.

## M7 (CB3-03, finding) — receipt lookup is answerable, but *recency* is not

"Does the evidence catalog index workspace-change receipts queryably?" — yes, weakly.
`EvidenceCatalog.list()` (`harness_labs/controller_evidence.py:107-110`) returns all records sorted
by ref; `EvidenceRecord.kind` (controller_evidence.py:21) allows a linear filter on
`"workspace-change-receipt"`; this is already how all three existing selectors work. It is O(n) plus
a JSON parse per record; fine at CB-3 scale.

What the catalog **cannot** answer is order. Refs are `artifact:sha256:<digest>`
(controller_evidence.py:65), so `sorted(self._records)` is digest order — arbitrary. `EvidenceRecord`
has no timestamp and no sequence number. Consequences:

- `_best_covering_receipt`'s tie-break "ties broken by evidence ref for determinism"
  (agent_mixture.py:336-339) is deterministic but **not chronological**; the docstring at
  feature_run.py:1536-1538 claiming "a stale receipt from before further edits is passed over once a
  newer one covers everything" is only incidentally true (via the `extra` minimisation), not by
  recency.
- CB3-04's AC-CB304-1 condition "**no newer attempt has started**" is not decidable from the
  evidence catalog at all. Recency has to come from the audit journal ordinal (`audit_path`, e.g.
  `artifacts/000121-…`, controller_evidence.py:78-86) or from kernel task state. Say which.

On AC-CB303-2's "never union receipts": mechanically correct as stated, but the plan should own the
consequence it names in the prompt. When an implementer commits partial work across two attempts,
each attempt's receipt records the **cumulative** `final_workspace["changed_paths"]`
(controller_live.py:399, claude_task_executor.py:302), not that attempt's delta — the per-attempt
delta is the separate `worker_changed_paths` field. So the later receipt normally *does* cover both
attempts' paths and no union is needed. The union case only arises when a receipt is missing for one
of the attempts (see M9), which is the same hole. "Never union" is the right semantics; the plan
should state the reason (receipts are cumulative snapshots) rather than leaving it as an assertion.

## M8 (CB3-04, blocking) — the restoration mechanism as specified is **unimplementable**: receipts store hashes, not pre-images

AC-CB304-1: "Restoration reverts exactly the receipted paths to their **receipt-recorded pre-attempt
state**."

The receipt (`workspace-change-receipt/2`, written at controller_live.py:389-406 and
claude_task_executor.py:290-307) stores `baseline_changed_paths` and `baseline_files`, where
`baseline_files` is `initial_workspace["files"]` from `workspace_snapshot`
(`harness_labs/git_transaction.py:91-102`). Each entry is `_path_state`
(`harness_labs/git_transaction.py:364-379`):

```python
return {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size}
```

**Content is never recorded — only kind, sha256 and size.** A sha256 is sufficient to *verify* the
pre-attempt state (AC-CB304-1's "matching content state" test is fine) and categorically
insufficient to *reconstruct* it. As specified, CB3-04 cannot be built.

Two ways out; the plan must pick one explicitly.

**(a) Narrow the trigger to the git-reconstructible case (recommended, no format change).** The
receipt already stores `baseline_head` (controller_live.py:395, claude_task_executor.py:295). When
`baseline_changed_paths == []` the pre-attempt tree was clean at `baseline_head`, and restoration is
exactly `git checkout <baseline_head> -- <tracked receipted paths>` plus `rm` of receipted paths
absent from that tree, with the result re-verified against `baseline_files`. Add to AC-CB304-1 a
fifth conjunct: *the attempt's `baseline_changed_paths` is empty*. Attempts that themselves started
from an adopted dirty baseline are then explicitly non-restorable — state that as a limitation, not
silently.

**(b) Add pre-images to the receipt (larger, changes the keep-list surface).** Bump to
`workspace-change-receipt/3` and, for each path in `baseline_changed_paths` whose state differs from
`baseline_head`, store a git blob oid (via `git hash-object -w`) or an evidence-catalog ref to the
pre-image bytes. New writes go in `controller_live.py:389-406` and
`claude_task_executor.py:290-307` — both inside CB3-04's grant — but the reader/consumer changes and
the `/2` → `/3` compatibility shim touch `agent_mixture.py` and `feature_run.py`, which CB3-04 does
**not** own. Blob-oid form also needs a `git hash-object -w` write at receipt time, which is a new
side effect on a path the keep-list currently treats as read-only attestation.

## M9 (CB3-04, blocking) — the trigger's "receipted residue" conjunct is unsatisfiable in the CB2-05 specimen the red phase cites

Failed attempts frequently leave residue and **no receipt**, because the receipt `evidence.add` is
reached only after every scope check passes:

- `claude_task_executor.py:235-286` raise `LiveExecutionError` for timeout, error envelope, missing
  JSON, HEAD change, branch change, out-of-grant paths, `require_repository_change`,
  `forbid_repository_change` — all **before** the `evidence.add(kind=_WORKSPACE_CHANGE_RECEIPT_KIND)`
  at line 290. Only the deliverable-floor check (line 312) and later failures leave a receipt.
- Same ordering in `controller_live.py` (raises at 366-388, receipt at 389, floor at 409).

Checked against the journals:

| specimen | `workspace-change-receipt` artifacts |
| --- | --- |
| `contract-burden-relaxation-2-attempt-2-CB2-05` | **0** |
| `contract-burden-relaxation-2-attempt-3-CB2-08` | 1 |
| `contract-burden-relaxation-2-attempt-1-CB2-02` | 3 |

AC-CB304-4 promises the red phase reproduces "the CB2-05/CB2-08 specimens". CB2-05 produced no
receipt at all, so CB3-04's conjunctive gate would emit `restoration-declined` in precisely the
specimen it claims to fix, and the node would land green while the live failure mode stayed live.
Either drop CB2-05 from the citation and scope CB3-04 to the CB2-08 shape, or add a companion
change that writes an attested-residue receipt on the early-refusal paths (a `try/finally` receipt
around claude_task_executor.py:235-306 — inside the grant) so residue is always attested. The
latter is the one that actually retires item 19's dominant amplifier.

## M10 (CB3-05, blocking) — nothing hangs; the session was **deliberately aborted**, and the run already reached `blocked`

The plan's causal story ("a coordinator waiting on a human in an unattended run produces an
`error_during_execution` session death") does not match the machinery or the journal.

Machinery: `_operator_input_request` (controller_kernel.py:990-1000) validates two payload strings
and returns an `operator_input.requested` effect. It does not wait, poll, or block on anything.
`_apply` (controller_kernel.py:1320-1322) appends the question to `state["operator_questions"]` and
sets `self._state["status"] = "blocked"`. There is **no operator channel abstraction anywhere in the
codebase** — `grep -rn "operator_input\|operator_channel" harness_labs/ experiments/` returns only
the kernel command table (controller_kernel.py:33,549,574), the coordinator tool map
(`controller_coordinator.py:48,545`) and the projection's allowed-commands list
(`controller_projection.py:116`). CB3-05 must *invent* the channel concept, so AC-CB305-3's "attended
runs are byte-for-byte unchanged" is vacuous: there is no attended mode to preserve.

Journal (`contract-burden-relaxation-2-attempt-3-CB2-08/events.jsonl`, seq 133-141):

- seq 133-134: the `operator_input.request` is **accepted**, journaled as
  `controller-event-000009`, and the tool call returns. No hang.
- seq 137: `transport_message / error_during_execution`. The artifact
  `artifacts/000121-claude-stream-inbound.jsonl` shows `"stop_reason": "tool_use"`,
  `"terminal_reason": "aborted_streaming"`.
- seq 138: `backend_process_terminated`, `"returncode": 143` — **SIGTERM**.
- seq 139-141: `coordinator.session_ended` with `outcome: "blocked"`, then
  `run_failed / {"terminal_status": "blocked"}`.

So: not a timeout, not a hang, not an exception. The kernel's own `status = "blocked"` write causes
the dispatcher's loop guard (`coordinator_dispatcher.py:90` `if state["status"] != "running"`, and
`:435-438`) to end the segment, which SIGTERMs the resident session mid-turn — producing the
`error_during_execution` the plan misreads as the cause. **And the run already terminated `blocked`
with the question already preserved** in `state["operator_questions"]` and projected to the
coordinator (`controller_projection.py:105`).

That guts AC-CB305-2 ("terminal status is the same `blocked` … minus the session-error death") and
AC-CB305-4's red phase ("the run dies on session error rather than resolving to a typed response") —
the run does not die instead of blocking; it blocks, and the abort is the *consequence* of blocking.
A typed `operator_unavailable` response event does not change this: the status write at
controller_kernel.py:1321 still fires, the dispatcher still terminates the session, and the SIGTERM
still lands.

The real, narrow defect worth fixing: the coordinator is killed mid-turn and never gets to write its
own blocker narrative or a `run.block_request` with a diagnosable reason. Two candidate mechanisms,
both in-grant: (a) do not set `status = "blocked"` on `operator_input.requested` in headless mode —
instead return the typed `operator_unavailable` as a *tool result* so the coordinator's next turn can
issue `run.block_request` with the question ref in the reason; or (b) keep the status write but have
the dispatcher drain the current turn before terminating. (a) is cleaner and matches CB3-05's stated
shape once the causal story is corrected. Rewrite the objective and AC-CB305-1/2/4 accordingly — as
written, a competent implementer would build the typed response, observe the SIGTERM unchanged, and
have no AC to fail on.

## M11 (CB3-06, finding) — out-of-grant screening already exists; the real gap is the `contract_violation` / `requires_disposition` escape and the `file`-only anchor

AC-CB306-2 ("at ingest, a finding whose file or required_paths fall outside the node's writable paths
is not entered as an open obligation") is largely implemented:

- `review_fix.py:504-512` — the loop stamps `scope_expanding` from
  `paths_outside_scope(finding.get("required_paths", ()), self.allowed_paths)` *before* `ingest`.
- `review_fix.py:576-583` — `scope_expansion_guard_enabled` demotes such open findings to
  `scope_screened`, which drops them from `fix_keys` (review_fix.py:299-301).
- `review_fix.py:191-215` — `transfer_scope_expanding` reassigns them to a downstream owner.

Three genuine gaps:

1. **The guard is bypassed** when `record["contract_violation"]` or `record["requires_disposition"]`
   is set (review_fix.py:578-581). Such a finding stays `open`, and `open_required`
   (review_fix.py:341-347) selects exactly `open`/`pending_review` findings with those two flags — so
   even when `fix_keys` empties, `review_fix.py:536-542` returns `blocked / "required findings remain
   open"`. **This is the actual CB2-02 deadlock shape.** AC-CB306-2's "excluded from fix_keys and
   from cycle-limit blocking arithmetic" is incomplete: it must also say *excluded from
   `open_required()`*, otherwise the deadlock survives the fix.
2. **`file` is not consulted.** The stamp at review_fix.py:506-508 reads `required_paths` only. A
   finding anchored by `file` alone is never screened. AC-CB306-2 says "file or required_paths";
   that half is new work.
3. **Where the ledger learns the writable paths:** it does not. `allowed_paths` lives on the
   `ReviewFixLoop` (review_fix.py:451, 466), and `FindingLedger.ingest` (review_fix.py:217-308) has
   no access to it — today the loop pre-stamps `scope_expanding` and passes `current_paths` to
   `transfer_scope_expanding` (review_fix.py:517). Implementing AC-CB306-2 "at ingest" literally
   means plumbing `allowed_paths` into the ledger constructor; the cheaper in-place option is to keep
   the pre-stamp at review_fix.py:504-512 and change what the stamp *does*. Either is in-grant, but
   the plan should not say "at ingest" without saying which.

**Red-phase constructibility (AC-CB306-4):** constructible, but only if the seeded finding carries
`contract_violation=True` or `requires_disposition=True`. A plain out-of-grant finding is already
`scope_screened` at RED_BASE and will not recur to the cycle ceiling — that test would be green on
the base. The AC should name the flag, or the implementer will write a green "red" test.

## M12 (CB3-06, blocking) — "controller-owned (not worker-authored)" is not decidable from the evidence catalog, and the fix is out of grant

AC-CB306-1 requires the ledger to "verif[y] the artifact exists and is controller-owned (not
worker-authored)". `EvidenceRecord` (controller_evidence.py:18-37) carries `ref, kind, sha256,
media_type, producer_task_id, size_bytes, audit_path`. There is **no ownership field**, and
`producer_task_id` does not discriminate: the controller-minted workspace receipt
(controller_live.py:404) and the worker's own deliverable artifact (controller_live.py:414-419) are
both added with `producer_task_id=str(self.task["id"])`.

So ownership can only be inferred from `kind` — an allow-list such as
`{"workspace-change-receipt", "verified-command-output", "controller-command-receipt"}`. Note
`controller_live.py:268-272` already treats `artifact_kind == _WORKSPACE_CHANGE_RECEIPT_KIND` as a
kind a worker may not claim, which is the same idea; reuse it. The alternative — an `owner` field on
`EvidenceRecord` — requires editing `harness_labs/controller_evidence.py`, which is **not** in
CB3-06's owned paths (`review_fix.py` + tests only). Either add it to the grant or commit to the
kind allow-list in the AC. AC-CB306-3's "a worker cannot self-discharge by citing its own output
artifact" is the security-relevant half of this and currently has no mechanism behind it.

Composition with the rest of the loop is otherwise fine: a discharge-by-receipt verb sets
`record["outcome"]` to a non-`open` value, which automatically drops it from `fix_keys`
(review_fix.py:299-301), from `open_all()` (review_fix.py:349-354) and from `open_required()`
(review_fix.py:341-347); the `reraise_guard_enabled` set at review_fix.py:253-255 must gain the new
outcome or a rediscovered discharged finding will reopen (review_fix.py:259-260). Discovery freeze
(review_fix.py:238-247) is untouched — discharge is a post-ingest outcome transition, and the cycle
ceiling at review_fix.py:543-544 keys off `fix_keys`, so discharged findings shorten cycles rather
than perturbing the arithmetic. Name the `reraise_guard` set update in an AC; it is the one silent
break.

## M13 (all nodes) — red-phase constructibility summary

Program rule 6 ("red constructions live inside test methods/`setUp` only") is satisfiable: every
target is a plain callable or a dataclass, and the base collects `tests/` cleanly (445 passed).
ImportError-style false reds are avoidable for CB3-02/03/04/06 because
`_controller_dirty_baseline_grant`, `_resolve_dirty_baseline_grant`, `FindingLedger.ingest` and the
workspace receipt all exist at RED_BASE and can be driven behaviourally.

Per-node:

| node | red phase behavioural at RED_BASE? |
| --- | --- |
| CB3-01 | **No** — passes today (M1). Re-anchor on `task:`/`decision:` refs (M2). |
| CB3-02 | Yes for the `agent_mixture` divergence; **no** for the cited CB2-03 specimen (M5). |
| CB3-03 | Yes, if the fixture seeds a receipt-covered dirty tree and a coordinator dispatch. |
| CB3-04 | Yes as a *test*, but the green phase is unimplementable as specified (M8) and the cited specimen is out of gate (M9). |
| CB3-05 | Ambiguous — the base does not hang (M10); a literal reading yields a red on "no `operator_unavailable` event exists", which is an ImportError-in-spirit test (asserting absence of an unimplemented symbol), not a behavioural failure. |
| CB3-06 | Yes, with `contract_violation=True` on the seeded finding (M11). |

## M14 (program rules) — budgets are sane, one number is stale

Measured at RED_BASE: full suite **61.2 s** wall (445 passed, 1 skipped), not the ~52 s the plan
states — a 17% understatement, immaterial against the budgets but worth correcting since rule 3
makes the baseline load-bearing for the "third consecutive timeout" heuristic. Node gate
`--timeout 1400` ≈ 23× the suite; `verification_timeout_seconds 3600` ≈ 59×. Both are hang detectors
rather than throughput limits, which is the stated intent. The plan does not mention the 1 skipped
test; a node that accidentally un-skips or hard-skips it would move a number reviewers are told to
compare against. `max_parallelism = 2` against the dependency graph gives roots {01,02,05,06} in two
pairs, then {03,04}, then 07 — five waves, consistent.

---

## Self-refutations

**R1 — M1/M2 could be read as "CB3-01 is unnecessary".** It is not. The item-6 defect is live; my
claim is only that the plan's *mechanism* targets the already-landed half. The 4 `task:` and 1
`decision:` refusals in the corpus are real and unfixed, and they are fixable inside CB3-01's
existing grant. The node should be re-scoped, not deleted. NECESSITY may reach a different verdict
on the discoverability half (M2's second paragraph), which is arguably cosmetic.

**R2 — M8 may be too strong.** I claim restoration is unimplementable; strictly, it is
unimplementable *for the general case*. Option (a) covers what I would guess is the large majority of
real failures (an attempt starting from a clean tree), and CB3-04 could ship usefully with that
narrowing and never notice the gap. My "blocking" label rests on AC-CB304-1's unqualified wording —
if the authors intended (a) all along, this is a wording fix, not a redesign. I did not sample enough
receipts to say what fraction have empty `baseline_changed_paths`.

**R3 — M10 rests on a single specimen.** I traced `attempt-3-CB2-08` end to end and confirmed
SIGTERM/`aborted_streaming`. I did *not* trace the other ten journals containing `operator_input`
(the grep in this review counted occurrences only). If any of them shows a genuine wall-clock hang or
a timeout kill, CB3-05's premise is partially rescued and my "blocking" should soften to "the
objective's causal story is at best one of two mechanisms". The absence of any operator-channel code
in `harness_labs/` is independent of this and stands either way.

**R4 — M5's grant-boundary objection assumes the CB2-03 citation is load-bearing.** If the authors
are content to reproduce the *class* of divergence via `agent_mixture` and treat feature_run's copy
as follow-up work, CB3-02 is coherent as written minus the specimen citation. I flagged it blocking
because AC-CB302-4 names the specimen explicitly and program rule 1 makes red-tail evidence the
discharge mechanism — an implementer will try to reproduce exactly what the AC names.

**R5 — M11 might overstate the `contract_violation` escape.** I read the guard chain statically and
did not construct the deadlock. It is possible that in the CB2-02 run the out-of-grant finding was
*not* flagged `contract_violation` and deadlocked by some other route, in which case my "real gap" is
mislocated even though the escape hatch at review_fix.py:578-581 is genuinely there. I did not open
the CB2-02 ledger artifact to check the flags on the actual finding.

**R6 — M12's ownership objection may be moot.** If `kind` is deemed a sufficient ownership proxy by
the authors, no `controller_evidence.py` edit is needed and the grant is fine. My finding then
reduces to "the AC should say `kind` allow-list, not 'controller-owned'". I rate it blocking only
because AC-CB306-3's anti-self-discharge property is a genuine integrity guarantee and should not
rest on an unstated inference.

**R7 — I did not run any of the finding tests**, because none exist yet. Every red/green judgement
here is static reading plus journal evidence. The one thing I executed is the base suite (M14).
