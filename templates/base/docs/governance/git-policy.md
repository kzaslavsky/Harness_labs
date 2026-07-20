# Git and pull-request policy

**Status:** Current

## Change classification

Classify each proposed change before merge:

- **Routine:** localized, reversible work with declared checks.
- **Consequential:** security, privacy, data contract/schema, public API,
  deployment, permission, or other high-blast-radius changes. When uncertain,
  treat a change as consequential.

## Pull requests and merge

Use a focused branch and pull request (or the repository’s equivalent review
mechanism). Describe scope, risk classification, verification evidence, and any
known limitations. Run the checks declared by this repository and confirm CI is
terminal-green before merging. Auto-merge configuration is not proof that checks
ran or passed.

Consequential changes require explicit operator approval before merge. This
policy sets portable principles; repository-specific branch, hosting, and command
details belong in a local addendum.
