# Load-flake: red_green_check timeout classification test (2026-08-19)

## Incident

During the convergence campaign (graph `convergence-campaign-harness`,
attempt-8), the graph's finalize gate (`pytest tests/ -q`) failed with
exactly one failure out of 903 tests:

```
tests/test_relax_gate_timeout_classification.py::RelaxGateTimeoutClassificationTests
  ::test_green_phase_timeout_exits_124_with_top_level_timed_out_marker
AssertionError: 'red-phase-timeout' != 'green-phase-timeout'
```

Evidence: `logs/runs/cc-graph/convergence-campaign-harness-attempt-8/
artifacts/000010-plan-graph-functionality-failure-evidence.json`
(`artifact:sha256:1353609ac218db456fc23ed578e395c75a4c45a5e6ec9085ccb3ca570e4eba2e`).

The file was touched by no campaign candidate (zero hits in the
`e174fa2..a5284d3` diff), the suite passes in isolation on the joined
candidate (3/3), and the full suite passed on the same candidate on a quiet
machine (902 passed, 2 skipped). The graph attempt's recorded terminal
status is therefore `failed` on environmental grounds only; the operator
accepted the sealed nodes on this evidence (option A, 2026-08-19).

## Root cause

The test drives `scripts/dev/red_green_check.py` with `--timeout 2` and a
`time.sleep(10)` regression test, expecting only the green phase to time
out. But the single `--timeout` budget applies to **each phase's whole
pytest invocation, interpreter startup included**. Under load (five
concurrent agent sessions plus a second campaign), the red phase — an
instant `assert False` probe — took longer than 2 s of wall clock to
*start*, so `run_pytest` reported the red phase timed out and the script
correctly answered `red-phase-timeout`. The script's classification logic
is sound; the test's timing assumption ("red always finishes inside the
same tight budget that must catch green's sleep") is what breaks under
load.

## Proposed fix

Give the two phases independent budgets instead of tightening the shared
one:

1. `scripts/dev/red_green_check.py`: add an optional `--red-timeout`
   (default: the value of `--timeout`, preserving existing behavior and
   the CLI contract for current callers). The red phase uses
   `--red-timeout`; the green phase keeps `--timeout`.
2. `tests/test_relax_gate_timeout_classification.py`: in
   `test_green_phase_timeout_exits_124_with_top_level_timed_out_marker`,
   pass `--red-timeout 60 --timeout 2`. The red probe still fails fast in
   practice; 60 s of headroom removes the startup-under-load sensitivity
   entirely, while the green phase keeps the tight budget the test needs
   to observe a green-phase timeout quickly. Test runtime stays ~2 s plus
   startup.
3. While there, the sibling red-phase-timeout test (if any) can pin its
   intent explicitly with `--red-timeout 2`.

Alternative considered and rejected: raising the shared `--timeout` — it
slows the test by the same amount for the green wait, and any single
shared budget re-encodes the race.

## Related gap (for the deferred table)

A single flaky finalize-gate test currently costs a full leaf-node re-run
to re-seal a graph: repair resume requires a non-empty retry frontier, so
there is no "re-verify the sealed graph" operation. Trigger fired
2026-08-19 (this incident: sealing attempt-9 would have re-implemented
CC-07 from scratch; the operator stopped it and accepted attempt-8's
evidence instead).
