---
description: Implement one feature with durable planning, review, verification, and handoff
argument-hint: [--base <branch>] <feature description>
---

# Implement v11

Implement exactly one feature through the repository's durable implementation
workflow. Treat `docs/development/current_implementation_checkpoint.json` and
`current_implementation_plan.md` as the source of truth when they exist.

1. Inspect repository instructions, current state, relevant source, tests, and
   existing learnings. Resolve the base branch from `--base`, the current branch, or
   the repository default; verify it exists.
2. Write a source-bound plan with affected files, contracts, validation, risks,
   ownership, and rollback/compatibility notes. Persist the checkpoint before each
   phase.
3. Use subagents liberally for independent plan review, implementation, and review
   work. Keep one coordinator responsible for integration and use disjoint file
   ownership for parallel writers.
4. Run phases in order: planning, plan review, implementation, runtime smoke,
   code review/fixes, then final verification. Re-run review after material fixes.
5. Treat mandatory validation failures, unavailable required credentials, and git
   guard failures as blockers. Escalate only irreversible, security, data-custody,
   public-contract, or other major architecture decisions; record ordinary decisions
   with their evidence.
6. Before handoff, run required tests, linters, documentation checks, and project
   safety gates. Stage only intended files and follow the repository git policy.

Report changed files, decision records, review outcomes, validation evidence, and
commit/merge state. Do not report completion without observed passing evidence.
