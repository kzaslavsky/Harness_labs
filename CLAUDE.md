# harness_labs

Operating standards for all agents: `AGENTS.md` (mission, repository map,
non-negotiable harness properties, run lifecycle, git authority, completion
standard). Follow it in full.

Campaign work (turning behavior statements into an approved, running
PlanGraph) goes through the delta-to-run pipeline — read
`docs/development/delta-to-run-agent-guide.md` first; its "Operational
gotchas" section lists the failure modes that have each already cost a real
blocked attempt.

Quick rules that bite hardest:

- Work only in dedicated worktrees; `main` receives merges only; the base
  must stay pristine (including untracked files) whenever a PlanGraph node
  launches or resumes. Log only under gitignored paths (`logs/runs/`,
  `logs/plan-approval/`).
- Full-suite gate: `python3 -m pytest tests/ -q`.
- Import layering: nothing under `harness_labs/core` imports
  `harness_labs.plangraph`; `plangraph` never imports `graphrun`
  (`tests/test_import_boundaries.py`).
- `canonical_plan_graph_payload` has a closed top-level key set — never
  invent payload fields.
- No real-browser dependency in CI: stub driver + `UI_FIDELITY_PYTHON`
  skip-with-recorded-reason.
