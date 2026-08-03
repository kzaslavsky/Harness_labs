# Parallel child dispatch implementation plan

Status: implemented
Date: 2026-08-02

## Repository identity

- Feature worktree:
  `/Users/kirillzaslavsky/Documents/harness_labs-parallel-child-dispatch`
- Feature branch: `codex/parallel-child-dispatch`
- Base branch: `Impl-redo`
- Base commit: `e677f03e2a3a1ecf4d28bcf6630fd613c70c174d`

## Objective

Make bounded parallel child dispatch part of the attempt runner composition, then
repeat the Retinology worktree survey through one resident Codex parent and one
fresh read-only Codex child per registered worktree.

## Acceptance criteria

1. A batch is fully authorized before any child starts.
2. Independent children overlap and never exceed the configured cap.
3. Child IDs and returned results preserve request order even when completion
   order differs.
4. One child crash is represented as a failed terminal result and does not
   cancel peers under the `collect_all` policy.
5. The provider adapter receives one generic `spawn_children` call; scheduling
   remains controller-owned.
6. The parent Codex app-server process remains resident while children work.
7. Events, batch membership, results, and checkpoint recovery are auditable.
8. The live survey dispatches every registered Retinology worktree exactly once
   and produces a parent-collated report.

## Review

The main risks are shared mutable executor instances, nondeterministic result
ordering, checkpoint lost updates, hidden partial launch after invalid input,
and a model omitting worktrees. The implementation addresses these with unique
batch roles, serial reservation before launch, indexed result collection,
serialized dispatcher audit updates, journal-ahead reconciliation, and an
optional exact-role-set gate on the parent tool.
