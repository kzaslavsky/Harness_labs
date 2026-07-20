# Plan Review Log

**Status:** Complete
**Plan:** `current_implementation_plan.md`
**Reviewed:** 2026-07-14

## Round 1 — independent architecture, risk, security, and source-binding pass

| Severity | Finding | Evidence | Mitigation integrated |
|---|---|---|---|
| HIGH | Package skill folders were described as import sources, so generated projects would not actually load the requested skills. | The plan originally specified a project-local inventory only. Retinology's Claude commands are loader-facing `.claude/commands/*.md`; Codex skills use `<name>/SKILL.md`. | The initializer now defaults to copying Claude commands to `.claude/commands/` and Codex skills to `.agents/skills/`, with an explicit `--skill-surface` contract and tests. |
| HIGH | A `templates/base/module/` directory would be copied as an ordinary base-overlay path, leaking template docs into every project root. | The planned copier copies `templates/base` wholesale while the module templates were under that root. | Module templates now reside at `assets/module/` and are copied only for validated `--module` inputs. |
| HIGH | The ported capture-learning skill would invoke Retinology's regeneration utility, but no portable replacement was planned. | `Retinology/.claude/commands/capture-learning.md` invokes `learnings/scripts/regenerate_md.py`. | The plan requires either a portable replacement utility or a consistently rewritten direct-update contract, plus an end-to-end test. |
| HIGH | Merge/overwrite, token rendering, and symlink behavior were unspecified, allowing unsafe writes or malformed generated configuration. | The original contract allowed an unspecified overwrite/merge mode and generic token replacement. Retinology's review guidance explicitly treats traversal and symlink following as security concerns. | Version 1 now rejects all existing/symlink targets, rejects template/destination symlinks and traversal, requires preflight confinement, prohibits hooks, and mandates format-aware token rendering tests. |

## Round 2 — adversarial re-review

| Severity | Finding | Evidence | Mitigation integrated |
|---|---|---|---|
| HIGH | The planned Codex loader location, `.codex/skills`, was not source-backed and would not make repo skills discoverable. | The current Codex manual's **Build skills → Where to save skills** section states that Codex scans repo skills from `.agents/skills` and defines the required `<name>/SKILL.md` layout. | Generated Codex skills now target `.agents/skills/<name>/SKILL.md`; the package source folders remain separate under `skills/codex/`. Validation asserts the documented repository layout. |

## Round 3 — final adversarial re-review

**Verdict:** pass. The revised plan has documented loader locations, asset separation, learning-capture support, manifest collision rules, input/rendering constraints, and testable filesystem-safety boundaries. No unresolved CRITICAL or HIGH finding remains.
