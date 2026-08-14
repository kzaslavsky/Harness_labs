# Harness Labs — Agent Operating Contract

Status: active

## Mission

Harness Labs designs coding harnesses for autonomous feature development. A harness must combine static, repository-owned policy with dynamic,
run-specific orchestration in a hierarchical agent architecture.

The optimization target is accuracy multiplied by efficiency. Accuracy is a
release gate, not a quantity that may be traded away to make a run look cheap.
Compare efficiency only across runs with equivalent acceptance criteria.

## Repository map

- Public project orientation: `README.md`
- Normative harness contract: `docs/architecture/harness-contract.md`
- Context engineering contract: `docs/architecture/context-engineering.md`
- Logging and metrics: `docs/observability/logging-and-metrics.md`
- Architectural decisions: `docs/decisions/`
- Active development navigation: `docs/development/INDEX.md`
- Machine-readable contracts: `schemas/`
- Run output: `logs/runs/<run-id>/`
- Bootstrap initializer retained from the seed project: `bin/`, `templates/`,
  `assets/`, and `skills/`

## Non-negotiable harness properties

Every production harness must provide:

1. A static plane: versioned instructions, schemas, role definitions, quality
   gates, permission boundaries, and recovery rules.
2. A dynamic plane: runtime decomposition, worker selection, scheduling,
   context assembly, retries, escalation, and evidence-based stopping.
3. A bounded hierarchy: one accountable run owner, explicit parent/child task
   relationships, bounded subagent count and depth, and a single integration owner.
4. Explicit contracts for tasks, context packets, worker results, artifacts,
   decisions, verification, checkpoints, and integration.
5. Durable, structured logs and decision records sufficient to reconstruct why
   a run acted, stopped, retried, committed, or merged.
6. Isolated execution in a dedicated Git worktree on a dedicated branch.
7. Verification and review gates tied directly to stated acceptance criteria.

No required contract may exist only in a prompt or in an agent's memory.

## Working method

- Inspect before changing. Preserve unrelated work and bind material repository
  claims to a file, commit, or command result.
- Define acceptance criteria before implementation. Keep requirements,
  implementation ownership, and verification evidence traceable.
- Give each worker the smallest sufficient context packet. Include objective,
  scope, constraints, relevant files or symbols, acceptance checks, permissions,
  output schema, and escalation conditions.
- The prototype `ChildRequest.context` string is a pass-through bootstrap value,
  not a production context packet or authority boundary. Keep capabilities and
  workspace grants separate until packet validation is implemented.
- Keep agent responsibilities and writable paths disjoint when work runs in
  parallel. A parent remains accountable for validating child output.
- Record material alternatives, assumptions, deviations, retries, and deferrals.
  Do not log routine low-impact choices as decisions.
- Treat worker claims as untrusted until supported by diffs, artifacts, or
  verification output.
- Prefer deterministic checks and structured outputs over prose coordination.
- Update documentation and contracts in the same change as behavior.
- Never place credentials, private prompts, secrets, or sensitive user data in
  logs, metrics, fixtures, or context packets.
- **CRITICAL**: No generalized snapshot framework or unrelated refactor when reviewing changes. **CRITIAL**
- **CRITICAL**: bounded work only

## Required run lifecycle

A feature run progresses through `orient -> plan -> implement -> verify ->
review -> integrate -> report`. It may enter `blocked`, `failed`, or `recovering`
from any active phase. Every transition emits a structured event and updates a
durable checkpoint. Resume from the last verified checkpoint; do not infer state
from chat history.

Dynamic agent spawning must be justified by independent work, bounded by the
harness limits, and represented in the task tree. Review and integration must
remain independent of the worker's self-report for material changes.

## Git authority and integration

Harness runs operate in isolated worktrees and branches. Before editing, record
the worktree path, feature branch, base branch, and base commit.

Agents are authorized by repository policy to create branches and worktrees,
commit their scoped changes, and merge a completed feature branch into its
recorded base branch when all declared gates pass. This standing authorization
does not override a user's narrower instruction or platform safety controls.

Before merging, the integration owner must verify:

- the base branch and commit are the intended integration target;
- the feature branch contains only in-scope changes;
- required tests, contract checks, and review gates passed with recorded output;
- unresolved conflicts, critical findings, and required decisions are absent;
- the merge is non-destructive and does not rewrite shared history.

Never force-push, bypass a required gate, silently resolve a semantic conflict,
or claim merge success without reading back the resulting base commit. If the
base advanced, revalidate the integration result against the new base.

## Observability and optimization

Use the event and decision schemas in `schemas/`. Each run writes append-only
JSONL events and a final summary under `logs/runs/<run-id>/`. Metrics must include
their units, denominators, task-suite identity, and collection method.

At minimum, measure outcome correctness, gate pass rate, escaped defects,
rework, elapsed time, agent time, token use, tool calls, retries, diff churn,
parallelism, and integration latency. Optimize on a Pareto frontier and use the
accuracy-efficiency composite only for comparable workloads. Never optimize a
proxy in a way that weakens required verification.

## Completion standard

Work is complete only when the requested artifact exists, its relevant checks
pass, structured evidence is recorded, documentation is synchronized, and the
Git state matches the promised handoff. Report what changed, what ran, what did
not run, and any residual risk.
