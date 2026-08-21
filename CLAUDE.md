# Harness Labs — Claude session guidance

The operating contract for all agents is `AGENTS.md`; follow it.

Quick pointers:

- Engineering-memory features (gate-evidence `warnings` vs `notices`,
  required-paths impact warnings, cross-campaign finding history, decision
  registry) — read `docs/development/engineering-memory-agent-guide.md`
  before authoring decompositions, editing `plan_approval.py` /
  `convergence_ledger.py`, or writing ADRs.
- Active development navigation: `docs/development/INDEX.md`.
- Work only in dedicated worktrees under `.claude/worktrees/`; never edit
  the primary checkout. `main` receives merges only.
