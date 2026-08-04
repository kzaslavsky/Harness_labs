# Q12 observed-issue repair scope

Status: active bounded repair scope

This repair is limited to the eight observed failures below. Work may leave this
scope only to correct a regression introduced by these repairs.

1. Release scanning rejects the untracked runtime database `l2/l2.db` and
   gitleaks falsely classifies `resume_token_sha256` queue hashes as API keys.
   Operator disposition: ignore `l2/l2.db`; treat the named queue hash finding
   as a false positive without weakening other secret detection.
2. Q12 mounts six operations in every runtime variant, but
   `tests/test_current_ui_graph.py` retains the pre-Q12 counts. Current expected
   counts are `162/170/163/171`.
3. A worker assigned `status=$?` under `zsh -lc`; `status` is read-only in zsh.
   The portable local variable is `rc`.
4. A Q11 development-plan link is broken. Q11 predates this harness, so no Q11
   context-reconciliation artifact may be assumed. Historical-link rediscovery
   is permitted only when the broken link is a required completion gate.
5. The parent session reported that the planner remained active because the
   foreground `start_planning.py` process was still open after chaining into
   `run_feature.py`. Process liveness must not be reported as phase state.
6. The Q12 archived implementation plan does not link its Q12 decision record.
7. A compound diagnostic `rg` command returned 1 when one optional lookup had
   no matches. The worker continued; this was not a stall.
8. A coordinator used GNU `find -printf` on macOS BSD `find`, then recovered
   with `rg --files`.

Required outcome: repair these exact defects and add issue-specific regression
tests. Do not introduce a general command language, general link resolver, or
generalized release-gate framework.
