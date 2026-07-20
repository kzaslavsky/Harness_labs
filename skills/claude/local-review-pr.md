---
description: Perform an evidence-backed review of committed branch changes before a pull request or merge
argument-hint: [revision or range]
---

# Local review (PR)

Review committed changes in an explicit revision or range. Default to the merge-base
between the repository's configured base branch and `HEAD`, then review that range to
`HEAD`.

1. Validate the range, show its commits and diff summary, and read applicable
   `AGENTS.md` files.
2. Use subagents liberally for independent compliance, correctness, security,
   language quality, and cross-reference review. Chunk very large ranges by
   independent file groups.
3. Cross-reference claims made in changed documentation: paths, symbols, links,
   status labels, and numerical statements must match the current tree or identified
   source.
4. Verify and score every finding. Exclude pre-existing, linter-only, unsupported,
   or low-confidence findings; report findings at 80+ confidence and separate 95+
   critical issues.
5. Provide `file:line`, category, impact, supporting evidence, and an actionable fix
   for every finding.

Report the exact range, commit/file/line counts, findings, threshold, and any review
limitations. This command reviews; it does not change code or merge branches.
