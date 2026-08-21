# Harness Labs — Claude session guidance

The operating contract for all agents is `AGENTS.md`; follow it.

Quick pointers:

- Campaign work (turning behavior statements into an approved, running
  PlanGraph) goes through the delta-to-run pipeline — read
  `docs/development/delta-to-run-agent-guide.md` first; its "Operational
  gotchas" section lists failure modes that each already cost a real
  blocked attempt.
- Engineering-memory features (gate-evidence `warnings` vs `notices`,
  required-paths impact warnings, cross-campaign finding history, decision
  registry) — read `docs/development/engineering-memory-agent-guide.md`
  before authoring decompositions, editing `plan_approval.py` /
  `convergence_ledger.py`, or writing ADRs.
- Active development navigation: `docs/development/INDEX.md`.

Quick rules that bite hardest:

- Work only in dedicated worktrees under `.claude/worktrees/`; never edit
  the primary checkout. `main` receives merges only. The base must stay
  pristine (including untracked files) whenever a PlanGraph node launches
  or resumes; log only under gitignored paths (`logs/runs/`,
  `logs/plan-approval/`).
- Full-suite gate: `python3 -m pytest tests/ -q`.
- Import layering: nothing under `harness_labs/core` imports
  `harness_labs.plangraph`; `plangraph` never imports `graphrun`
  (`tests/test_import_boundaries.py`).
- `canonical_plan_graph_payload` has a closed top-level key set — never
  invent payload fields.
- No real-browser dependency in CI: stub driver + `UI_FIDELITY_PYTHON`
  skip-with-recorded-reason.
