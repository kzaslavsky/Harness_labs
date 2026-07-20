---
name: module-docs
description: Create or update a repository module's required context.md and API.md using source, callers, tests, and learning records. Use when documenting a module or refreshing module navigation/API references.
---

# Module documentation

Create or update both `context.md` and `API.md` in the requested module.

1. Read applicable `AGENTS.md` files, module source, tests, callers, existing docs,
   and relevant `learnings/` entries.
2. Derive content from repository evidence. Do not invent exports, dependencies,
   behavior, or examples.
3. Keep `context.md` compact and navigational: purpose, boundaries, decision guide,
   important pitfalls, key entry points, dependencies, and related docs.
4. Make `API.md` the complete public-interface reference: symbols, signatures,
   inputs/outputs, errors, configuration, and extended examples where useful.
5. Verify every link, file path, symbol, and example against the current tree.

Report changed files, verification evidence, and any facts that could not be
established from the repository.
