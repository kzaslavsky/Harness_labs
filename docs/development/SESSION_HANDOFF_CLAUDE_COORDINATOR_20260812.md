# Session Handoff — claude -p worker adapters landed; next: ClaudeAgentSession coordinator

**Date:** 2026-08-12 · **Worktree:** `~/Documents/harness_labs_feature_worktrees/claude-p-adapters` · **Branch:** `claude-p-adapters` (branched from `Impl-redo` @ `60b2c7c`) · **HEAD:** `edb5452`

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

## NEXT TASK: `ClaudeAgentSession` — the coordinator seat

Goal: a Claude-backed implementation of the `AgentSession` protocol so `run_feature_worktree(session_factory=...)` can put Claude in the coordinator seat (the phase coordinator that "cannot read files or run commands; uses only typed controller tools").

### The contract to implement (`harness_labs/agent_sessions.py:96`)

```
capabilities -> BackendCapabilities   # frozen transport description
open(ModelRequest) -> session_id      # ModelRequest: task, context, tools: tuple[ToolSpec,...], unavailable_tool_response
step(session_id, tool_result: ToolResult | None) -> ToolCall | FinalOutput | BackendFailure
close(session_id)
```

`SessionToolExecutor` (same file) drives the loop: it opens the session, calls `step()` repeatedly, executes each `ToolCall` itself (child dispatch etc.), and feeds the `ToolResult` back into the next `step()`. The session never executes tools — it only surfaces the model's tool intents and final output. `FinalOutput.usage` carries the normalized `Usage`.

### Reference implementations, in order of usefulness

- **`harness_labs/omlx_agent_session.py` (`OmlxAgentSession`)** — the simpler shape: message-list state per session, one HTTP call per `step()`, tool calls parsed from the response, tool results appended as messages. Behaviorally closest to what a claude stream-json bridge looks like.
- **`harness_labs/codex_agent_session.py` (`CodexAppServerSession`)** — the resident-subprocess shape: one long-lived `codex app-server --stdio` process, JSON-RPC framing, reader threads, `_SessionState`, executable SHA-256 identity artifact, audit events per step. Steal its process-lifecycle hygiene (`_cleanup`, stderr drain, `_require_state`).
- **`tests/test_agent_sessions.py`** — how sessions are exercised without real processes; `test_omlx_transport_emulates_two_tool_turns` is the pattern to mirror for multi-turn tool loops.

### The central design decision (resolve before coding)

`ModelRequest.tools` are **controller-defined tools whose execution must round-trip through `step()`**. Claude Code executes its own tools internally — including MCP tools — so the naive approach (register controller tools as an MCP server) inverts control: claude would block awaiting the MCP server's reply while the harness blocks awaiting `step()`. Two viable architectures:

1. **Resident `claude -p --input-format stream-json --output-format stream-json` + loopback MCP bridge owned by the session object.** `ClaudeAgentSession` hosts a minimal loopback HTTP MCP server (stdlib only, matching repo conventions — this repo deliberately has zero third-party deps, `urllib` not `requests`). Each controller `ToolSpec` is exposed as an MCP tool; the bridge handler *blocks* until the harness supplies the matching `ToolResult` via `step(session_id, tool_result)`, then returns it to claude. `step()` = read claude's stream until either an MCP tool-call event for one of our tools (→ return `ToolCall`) or the `result` message (→ `FinalOutput`). Flags: `--tools ""` (no built-ins), `--mcp-config <bridge>`, `--strict-mcp-config`, `--setting-sources ""`, `--effort`, per-session `--session-id`. Caveats: MCP tool-call/tool-result events must be observable in the stream (verify with `--include-partial-messages` or the default stream events — **test this live before committing to the design**); MCP client timeout config (`MCP_TIMEOUT`/`MCP_TOOL_TIMEOUT` env) must exceed the longest child dispatch.
2. **Claude Agent SDK (`claude-agent-sdk` Python package) with in-process `create_sdk_mcp_server` tools.** Handlers run inside the harness process, so blocking/unblocking is plain Python. Cleaner control flow, but adds the repo's first third-party dependency and an async runtime. If chosen, isolate it in one module and keep the import lazy so the rest of `harness_labs` stays stdlib-pure.

The stdlib-purity convention argues for (1); the control-flow simplicity argues for (2). Either way, prototype the tool round-trip against the real CLI *first* (one cheap `haiku` run, like the smoke tests that pinned the `-p` envelope) — the stream-json event shapes for MCP tool calls are the one thing this handoff could not verify.

### Capabilities mapping (initial position, adjust to reality)

`persistent_sessions=True` (resident process or `--resume`), `native_tool_calls=True` (via MCP bridge), `resumable_sessions` = whether you implement `--resume`/`--fork-session` (start `False`), `cached_input_reporting=True` (envelope reports cache reads), `structured_output=True` (`--json-schema`).

### Wiring and acceptance

- A FeatureRun script should be able to swap `CodexAppServerSession` → `ClaudeAgentSession` in its `session_factory` with no other changes (see `experiments/run_archimedes_feature.py:349` for the current factory shape and `BASE_INSTRUCTIONS` coordinator constraints).
- Audit parity with the codex session: executable identity artifact, per-step transport events with usage, prompt/stdout/stderr artifacts.
- Tests: fake the claude subprocess (or SDK client) the way `test_agent_sessions.py` fakes transports; cover open→tool-call→tool-result→final-output, backend failure surfacing, and close/cleanup. Extending `agent_mixture` to place `claude` in the *coordinator* seat (today it only covers workers) is in scope if it stays small.
- Before proposing the merge back to the canonical base: run the full suite in this worktree (`python3 -m pytest tests/` — expect 273+ green) and do one live end-to-end `haiku` coordinator smoke.

## Cautions

- `experiments/run_*_feature.py` scripts insert `HARNESS_LABS_SOURCE` (default: a *different* worktree, `review-fix-ledger`) into `sys.path` — when live-testing from this worktree, set `HARNESS_LABS_SOURCE` to this worktree's path or you will import someone else's harness.
- Worker-seat live smokes cost real tokens; use `--model haiku`-tier and `--max-budget-usd` where sensible.
- `Impl-redo` tracks `origin/featureRun` with many unpushed commits; pushing is the user's call, never yours.
