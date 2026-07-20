---
name: local-review
description: Review uncommitted repository changes for compliance, correctness, security, and quality using evidence-backed findings. Use before committing or when asked to review the current working-tree diff.
---

# Local review

Review `git diff HEAD`, including staged changes. If no changes exist, report that
fact and stop.

1. Read applicable repository instructions and inspect the diff plus enough source
   context to make each claim verifiable.
2. Use subagents liberally for independent compliance, correctness, security, and
   language-quality review. Review agents must not modify source.
3. Verify every finding against the current tree. Exclude pre-existing problems,
   linter-only findings, unsupported claims, and style nits without policy support.
4. Score confidence from 0–100. Default to reporting 80+ findings; treat 95+ as
   critical.
5. For each reported issue give `file:line`, category, impact, evidence, and a
   concrete suggested fix. Say explicitly when no qualifying issue was found.

Report reviewed scope, findings, threshold, and verification limitations. Do not
apply code changes as part of the review.
