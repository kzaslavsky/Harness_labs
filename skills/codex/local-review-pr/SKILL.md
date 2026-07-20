---
name: local-review-pr
description: Review a committed branch revision range before a pull request or merge, including verification of changed documentation claims. Use for PR-preparation reviews, committed-diff review, or branch-range code review.
---

# Local review (PR)

Review an explicit revision or range. Default to the range from the configured base
branch's merge-base through `HEAD`.

1. Validate the range, show commits and diff summary, and read applicable repository
   instructions.
2. Use subagents liberally for independent compliance, correctness, security,
   language quality, and cross-reference review. Split very large ranges into
   independent file groups.
3. Verify documentation claims changed by the range: paths, symbols, links, status
   labels, and numerical statements must match the current tree or identified source.
4. Verify and score every finding. Exclude pre-existing, linter-only, unsupported,
   or low-confidence findings; report 80+ findings and distinguish 95+ critical
   issues.
5. Provide `file:line`, category, impact, evidence, and an actionable fix for every
   finding.

Report the exact range, commit/file/line counts, findings, threshold, and any review
limitations. This skill reviews; it does not change code or merge branches.
