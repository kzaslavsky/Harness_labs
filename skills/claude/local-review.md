---
description: Perform an evidence-backed review of uncommitted changes
argument-hint: [--report-threshold <0-100>]
---

# Local review

Review `git diff HEAD` (including staged changes) before committing. If there are no
changes, report that fact and stop.

1. Read applicable `AGENTS.md` files and inspect the diff plus enough surrounding
   source to establish context.
2. Use subagents liberally for independent compliance, correctness, security, and
   language-quality review; keep review-only agents from modifying source.
3. Verify each finding against the current tree. Exclude pre-existing issues,
   linter-only findings, unsupported claims, and stylistic nits without a repository
   basis.
4. Score confidence from 0–100; default to reporting findings at 80 or above, with
   critical findings at 95 or above.
5. For every reported finding provide `file:line`, category, impact, evidence, and a
   concrete suggested fix. State explicitly when no qualifying issue was found.

Do not run implementation changes as part of this command. Report the reviewed scope,
findings, threshold, and any verification limitations.
