---
description: Create or refresh concise module context.md and API.md documentation
argument-hint: <module path>
---

# Module documentation

Create or update `<module>/context.md` and `<module>/API.md` for the requested
module. Both files are required for every module.

1. Read the repository's `AGENTS.md`, module source, tests, callers, existing docs,
   and relevant entries in `learnings/`.
2. Derive facts from source; do not invent exports, dependencies, or examples.
3. Write `context.md` as a concise navigation guide: purpose, boundaries, quick
   decision guide, important pitfalls, main entry points, dependencies, and related
   docs. Prefer links over duplicated reference detail.
4. Write `API.md` as the complete public-interface reference: exported symbols,
   inputs/outputs, errors, configuration, and extended examples where useful.
5. Verify every link, file path, symbol, and example against the current tree.

Report changed files, verification performed, and documentation gaps that could not
be established from repository evidence.
