---
name: implement-v11
description: Implement one repository feature through a durable workflow of source-bound planning, adversarial review, coordinated implementation, verification, and guarded handoff. Use for implement-v11, autonomous feature implementation, or resuming an implementation checkpoint.
---

# Implement v11

Implement exactly one feature. Use the repository's implementation checkpoint and
plan files as the source of truth when they exist.

1. Inspect repository instructions, current state, relevant source, tests, and
   learnings. Resolve and verify the intended base branch.
2. Persist a source-bound plan before implementation: affected files, contracts,
   validation, risks, ownership, compatibility, and rollback notes.
3. Use subagents liberally for independent plan review, implementation, and code
   review. Keep one coordinator for integration and assign parallel writers disjoint
   file ownership.
4. Run phases in order: planning, plan review, implementation, runtime smoke, code
   review/fixes, and final verification. Re-review after material fixes.
5. Block on mandatory validation failures, required credentials/permissions, or git
   guards. Escalate only irreversible, security/data-custody, public-contract, or
   major architecture decisions. Log ordinary decisions with evidence.
6. Run all required tests, linters, documentation checks, and safety gates before
   handoff. Follow the repository git policy and stage only intended files.

Report changed files, decisions, review outcomes, validation evidence, and
commit/merge state. Never claim completion without observed passing evidence.
