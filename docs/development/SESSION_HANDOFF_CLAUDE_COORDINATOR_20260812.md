# Session Handoff — claude -p worker adapters + ClaudeAgentSession coordinator landed

**Date:** 2026-08-12 · **Worktree:** `~/Documents/harness_labs_feature_worktrees/claude-p-adapters` · **Branch:** `claude-p-adapters` (branched from `Impl-redo` @ `60b2c7c`) · **HEAD:** `ec53bd4`

## Standing rules (user-stated, non-negotiable)

- **Never work in the primary checkout** (`~/Documents/harness_labs`), regardless of branch. All work happens in a dedicated worktree on its own branch; `main` (in practice: the canonical base branch, currently `Impl-redo`) only receives merges.
- The primary checkout currently holds *unrelated* uncommitted dashboard/plangraph work and untracked `experiments/*` embedded git repos. Do not touch, stage, or clean any of it.
- The session shell pins its cwd to the primary checkout and resets after every command — prefix every command with `cd ~/Documents/harness_labs_feature_worktrees/claude-p-adapters &&`.

## What this branch already contains (commit `edb5452`)

Three pieces making FeatureRun/PlanGraph workers `claude -p` compatible, plus tests (29 targeted tests pass in this worktree; the identical content passed the full 273-test suite before relocation):

1. **`harness_labs/backends.py` → `ClaudePrintBackend`** — text backend mirroring `CodexExecBackend`. Runs `claude -p --output-format json --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence` in a temp dir; audits transport + usage; `last_usage` property. Live-verified against the real CLI (model `haiku`).
2. **`harness_labs/claude_task_executor.py` → `ClaudeSemanticTaskExecutor`** — worker `Executor` mirroring `CodexSemanticTaskExecutor` (same preflight/evidence/criteria/`validate_semantic_result` contract, same `_RAW_OUTPUT_SCHEMA` via `--json-schema`). Deliberate deltas, documented in the module docstring: read-only workers get only `Read,Glob,Grep` **and** every execution is snapshot-proven (Claude's sandbox is permission-layer, not OS-layer, so read-only mutation fails the attempt); workspace-write gets `Read,Glob,Grep,Edit,Write,Bash` + `--dangerously-skip-permissions` with the usual post-hoc grant enforcement.
3. **`harness_labs/agent_mixture.py`** — declarative mixture layer: `parse_backend_spec("claude:claude-opus-5@high")`, `WorkerRole` (fail-fast sandbox validation), `build_role_profiles(mixture=..., roles=...)` → `RoleProfile` tuple (resolution order: role name → profile_id → `"*"`). `task_with_artifact_kind` is now shared instead of copy-pasted per experiment script. Providers: `claude`, `codex`.
4. `harness_labs/usage.py` → `parse_claude_result_usage`; exports wired through `harness_labs/__init__.py`; tests in `tests/test_claude_task_executor.py`, `tests/test_agent_mixture.py`, plus three cases in `tests/test_backends.py`.

## Verified `claude -p` contract facts (live-tested 2026-08-11, do not re-derive)

- **`--bare` breaks auth on this machine** — it restricts auth to `ANTHROPIC_API_KEY`/apiKeyHelper and the user authenticates via OAuth. Use `--setting-sources ""` for config isolation instead.
- `--output-format json` envelope fields: `is_error`, `subtype`, `result` (string), `structured_output` (validated object, present when `--json-schema` is passed and satisfied), `usage` (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`), `total_cost_usd`, `permission_denials`, `num_turns`, `session_id`.
- Usage normalization (implemented in `parse_claude_result_usage`): harness `input_tokens` = uncached + cache_read + cache_creation; `cached_input_tokens` = cache_read only.
- Relevant flags confirmed in `claude --help`: `--effort <low|medium|high|xhigh|max>`, `--input-format stream-json`, `--output-format stream-json`, `--replay-user-messages`, `--session-id <uuid>`, `--resume`, `--fork-session`, `--mcp-config`, `--strict-mcp-config`, `--tools`, `--permission-mode`, `--dangerously-skip-permissions`, `--json-schema`, `--max-budget-usd`, `--no-session-persistence`.

## DONE (2026-08-12, commit `ec53bd4`): `ClaudeAgentSession` — the coordinator seat

Implemented in `harness_labs/claude_agent_session.py` as architecture (1) from the original handoff: resident `claude -p --output-format stream-json` plus a **stdlib loopback HTTP MCP bridge** owned by the session. Each controller `ToolSpec` is served as MCP tool `mcp__controller__<name>`; the `tools/call` handler blocks until `step()` delivers the matching `ToolResult`. No third-party deps.

- **The design-critical fact was live-verified before coding** (claude 2.1.226): the assistant `tool_use` event reaches stdout *before* the MCP reply is awaited (probe: tool_use at ~3.0s vs. bridge response at ~6.0s with a deliberately stalled handler), so the blocking bridge cannot deadlock. Architecture (2) (Agent SDK dependency) was never needed.
- **New live contract fact:** with `--json-schema`, claude emits the final object through an internal `StructuredOutput` tool call on the stream before the `result` envelope. The session skips that block (the envelope's `structured_output` carries the same object); do not treat it as an unauthorized tool.
- Flags used per session: `--tools "" --setting-sources "" --mcp-config <loopback> --strict-mcp-config --allowedTools mcp__controller__... --json-schema <answer> --system-prompt <base instructions> --no-session-persistence --effort <e>`; `MCP_TOOL_TIMEOUT` env is set to `tool_timeout_seconds` (default 24h) so long child dispatches don't trip the MCP client.
- Audit parity with `CodexAppServerSession`: executable SHA-256 identity artifact, prompt artifact, per-line inbound stream + outbound bridge `transport_message` events, stderr artifact and `backend_process_terminated` on cleanup.
- Mixture layer: `build_coordinator_session("claude:claude-opus-5@high" | "codex:gpt-5.6-terra@low", base_instructions=..., audit=...)` covers the coordinator seat with the same `provider:model[@effort]` vocabulary as workers; exported through `harness_labs/__init__.py`.
- Tests: `tests/test_claude_agent_session.py` uses a fake `claude` executable that genuinely speaks the bridge's HTTP MCP protocol and emits real stream-json (two-tool round-trip, unknown-tool refusal text, error/death/schema failures, mismatched results, identity/reopen checks). Full suite: **287 passed** in this worktree.
- Live acceptance: one end-to-end `haiku` coordinator smoke — `open` → `spawn_child` ToolCall → bridged ToolResult → `FinalOutput` with normalized usage (2638 in / 331 out) → clean close.

## REMAINING / NEXT

- `resumable_sessions=False` for now; `--resume`/`--fork-session` support is unimplemented.
- A FeatureRun script swap (`CodexAppServerSession` → `ClaudeAgentSession` or `build_coordinator_session(...)` in `session_factory`) has not been exercised against a full `run_feature_worktree` live run — only the direct session loop. That is the natural next live test (haiku-tier, `max_budget_usd` set).
- Merge back to the canonical base (`Impl-redo`) is the user's call; the suite is green and the live smoke has run.

## Cautions (unchanged)

- `experiments/run_*_feature.py` scripts insert `HARNESS_LABS_SOURCE` (default: a *different* worktree, `review-fix-ledger`) into `sys.path` — when live-testing from this worktree, set `HARNESS_LABS_SOURCE` to this worktree's path or you will import someone else's harness.
- Live smokes cost real tokens; use `--model haiku`-tier and `--max-budget-usd` where sensible.
- `Impl-redo` tracks `origin/featureRun` with many unpushed commits; pushing is the user's call, never yours.
