# Q12 observed-issue repair plan

This plan is intentionally surgical and is checked against
`q12-observed-issues.md` before and after each cluster.

## Cluster A — Retinology tree

1. Add `/l2/l2.db` to the repository ignore policy.
2. Add a narrow gitleaks allowlist that matches only the queue
   `resume_token_sha256` field representation; verify a representative real
   secret is still detected.
3. Update the four UI-graph runtime counts to `162/170/163/171` and run the
   focused graph tests/checker.
4. Add the Q12 decision-record backlink to the archived Markdown plan; keep the
   authoritative JSON unchanged because its closed schema has no link field.
5. Repair the Q11 link only if the link checker proves it blocks completion and
   only when exactly one archived Q11 target is discoverable by exact basename
   or, for renamed historical plans, by the target's Q-number plus `plan`.

## Cluster B — Harness behavior

1. Emit a flushed structured `controller.phase` event from
   `start_planning.py` when the planner completes and the foreground process
   enters `run_feature.py`.
2. Emit the same event when `run_feature.py` observes a durable checkpoint
   revision/phase transition. The event states that process liveness is not
   phase authority.
3. Require the parent contract to use a zero-timeout queue snapshot or read the
   checkpoint after the first unexplained 55-second interval. Permitted wording
   before reading state is “controller session remains open; phase unknown.”
4. Put the actual execution environment in coordinator and child guidance:
   macOS BSD tools, zsh, `rc` rather than `status`, `rg --files` rather than GNU
   `find -printf`, and `rg ... || true` only for explicitly optional discovery.
5. Add a narrow historical Markdown-link resolver used only after the normal
   link gate fails. It may repair only a missing relative link whose basename,
   or Q-number-plus-plan archive search, has exactly one repository match; zero
   or multiple matches remain blocked.
6. Require an implementation plan to link its recorded decision record before
   documentation gates.

## Verification

- Unit-test each exact command/environment rule in compiled prompts.
- Test structured phase output across planner-to-feature handoff and later
  checkpoint transitions.
- Test link fallback for unique, missing, and ambiguous targets.
- Test gitleaks false-positive suppression and real-secret retention.
- Run Q12 graph tests and document-link/decision gates.
- Finish with one scoped controller simulation and verify that changed paths map
  only to these eight issues or their tests/documentation.

## Re-ground checkpoint 1

- Cluster A remains limited to scanner policy, four UI counts, and the two
  demonstrated documentation links. The PHI gate, gitleaks, UI graph, and link
  checks now pass in the Q12 worktree.
- Cluster B remains limited to durable phase events, exact macOS/zsh prompt
  context, the one-link historical fallback, and the plan-decision validator.
- Focused tests passed; the remaining work is the production-controller E2E and
  the existing full harness suite. No unrelated mechanism has been added.

## Re-ground checkpoint 2 — final

- The production dispatch/startup E2E observed `PLANNING/plan_validate/ready`
  and `PLAN_REVIEW/revised_plan_validate/blocked` from structured checkpoint
  events while the same foreground process remained open.
- All 104 implement-v13 tests, all 32 queue-dispatch tests, the 21 focused
  Retinology release/UI tests, the PHI scan, and documentation/backlink checks
  pass.
- The final changed-path audit maps every new path to the eight observed issues
  or their tests/contracts. No repair added a timeout, generalized command
  language, generalized gate framework, or automatic ambiguous link rewrite.
- The final PHI scan initially rejected the new detector test's inline synthetic
  credential. The fixture now constructs that value only in a temporary test
  directory; the detector still catches it and the repository PHI scan is clean.
