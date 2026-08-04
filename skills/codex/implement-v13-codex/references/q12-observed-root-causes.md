# Q12 observed-issue root causes

This classification deliberately leans toward the harness when responsibility
is ambiguous.

| # | Primary class | Root cause |
|---|---|---|
| 1 | Harness/repository policy | Runtime DB placement and ignore policy were not aligned with the PHI gate. The secret scanner had no narrow allowlist for the known queue hash field. The model contributed by leaving a runtime artifact in-tree, but the release policy lacked the repository-specific exclusions. |
| 2 | Model | Q12 changed runtime enumeration without updating the existing count fixture. The harness test correctly detected the stale expectation; the implementation failed to close it. |
| 3 | Model, enabled by harness context | The worker used bash-style variable naming under the actual zsh execution environment. The harness did not state the shell portability constraint in child context. |
| 4 | Harness fallback gap | The broken Q11 link was not created by this harness, so Q11 reconciliation cannot be required retroactively. Current completion lacked a narrowly scoped way to rediscover a unique archived target when a historical link blocks the gate. |
| 5 | Harness | The foreground controller intentionally spans planner and feature phases, but it emitted no explicit durable phase transition for the parent. The parent inferred phase from shell-process liveness instead of the checkpoint. |
| 6 | Model plus harness validation gap | The Q12 worker omitted the decision-record backlink, and plan/document validation did not make that backlink a required closure condition. |
| 7 | Model, enabled by harness context | The worker treated diagnostic `rg` no-match exit 1 as a compound-command failure. The harness did not distinguish optional discovery probes from required assertions. |
| 8 | Model, enabled by harness context | The coordinator assumed GNU `find` extensions on macOS. The harness did not surface the BSD/macOS command environment in every coordinator and worker context. |

The release scanner, phase-reporting, historical-link fallback, decision-link
validation, and execution-environment context are harness responsibilities.
Updating the UI fixture and avoiding incorrect shell commands are direct model
responsibilities, with targeted harness guardrails added because these failures
are cheap to prevent.
