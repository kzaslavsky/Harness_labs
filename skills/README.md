# Optional agent workflows

This directory contains portable, opt-in workflows. The initializer copies only the
selected engine's files into a generated project; it does not assume that either
Claude Code or Codex is installed.

| Source | Destination in a generated project | Contents |
| --- | --- | --- |
| `claude/` | `.claude/commands/` | Claude Code slash-command prompts |
| `codex/` | `.agents/skills/<name>/` | complete project-local Codex skill folders |

Available workflows:

- `module-docs` — create or refresh a module's `context.md` and `API.md`.
- `capture-learning` — record a reusable, evidence-backed learning.
- `implement-v11` — run one feature through a durable implementation workflow.
- `local-review` — review uncommitted changes.
- `local-review-pr` — review a committed branch range before a pull request or merge.
- `implement-v13-codex` — run one feature through the durable Codex-native production lifecycle.

These are templates, not universal policy. Adapt commands, validation gates, branch
rules, and compliance language to the generated project's `AGENTS.md` and selected
deployment templates. Do not copy an engine-specific workflow into a project unless
that engine is actually used.
