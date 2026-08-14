# Harness Labs

Harness Labs is a research and engineering repository for building Codex coding
harnesses that can autonomously deliver repository features with high accuracy
and high operational efficiency.

The target architecture is hybrid:

- **Static:** versioned policies, contracts, schemas, role definitions, quality
  gates, permissions, and recovery rules live in the repository.
- **Dynamic:** a run decomposes work, builds minimal context, assigns bounded
  specialist agents, validates outputs, recovers from failure, and integrates a
  verified result.

Harnesses are hierarchical, evidence-driven, and durable. Each run has one
accountable owner, an explicit task tree, isolated Git worktree and branch,
structured event and decision logs, and a guarded path to commit and merge into
the recorded base branch.

## Package layout

```
harness_labs/
  core/           # substrate any harness reuses (kernel, executors, journals, evidence)
  featurerun/     # single-feature harness (imports core only; standalone by contract)
  plangraph/      # graph orchestration (imports core + featurerun)
  observability/  # metrics, catalog, dashboard (imports core only)
  graphrun/       # composition + operator surface (nothing imports it)
```

Boundaries are enforced by `scripts/dev/check_import_boundaries.py` and
`tests/test_import_boundaries.py`; see
`docs/development/GRAPHRUN_RESTRUCTURE_PLAN.md`.

## Motivation

The project aims to make reliable coding harnesses for autonomous
implementation of large, complex tasks. Everything is a work in progress.
The motivation is twofold:

1. enable auditable cross-platform development;
2. tame the overengineering tendency of frontier coding models.

## Current paradigm — GraphRun

`main` carries the integrated line (formerly `Impl-redo`): **GraphRun**, the
composition of the two harness layers:

1. **FeatureRun** — develops a single feature in an isolated worktree through
   plan → implement → review → integrate phases. A FeatureRun consumes roughly
   $3–5 in API costs in testing. FeatureRun functions as a standalone unit.
2. **PlanGraph** — decomposes a large task into a dependency graph of
   FeatureRuns (which inherit partial plans from the graph and skip their own
   plan phase), with approval-receipt admission, red/green verification gates,
   parallel dispatch, and resume-with-reuse recovery.

The `featurerun` and `plangraph` branches are historical snapshots of the two
layers from when they were maintained separately; they are not kept current.
The contract-burden program series (CB-1, CB-2, CB-3 — see
[docs/development/contract-burden-reduction.md](docs/development/contract-burden-reduction.md))
was developed and verified with GraphRun operating on itself.

## Design objective

The central optimization objective is **accuracy × efficiency**. Accuracy gates
are constraints; efficiency improvements are accepted only when correctness and
verification coverage remain equivalent or improve. Metrics exist to support
experimentation, diagnosis, and iterative harness improvement—not surveillance
or vanity reporting.

## Contracts

- [Harness architecture](docs/architecture/harness-contract.md)
- [Context engineering](docs/architecture/context-engineering.md)
- [Logging and metrics](docs/observability/logging-and-metrics.md)
- [Decision records](docs/decisions/README.md)
- [Development index](docs/development/INDEX.md)
- [Agent operating contract](AGENTS.md)

Machine-readable schemas live in [`schemas/`](schemas/). Runtime logs belong in
`logs/runs/<run-id>/` and are ignored by Git except for directory documentation.

## Current implementation

The repository now contains a production-shaped FeatureRun runtime in addition
to the original dependency-free initializer.

The first runtime primitive is the dependency-free
[`TaskAttempt` runner](harness_labs/attempts.py). It invokes one replaceable
executor and accepts only a typed result whose identity and status validate.
The dependency-free [`TextExecutor`](harness_labs/text_executor.py) is the first
concrete executor: it resolves the attempt's task, context, and capability grant,
then delegates generation to a replaceable text backend. The reusable backend
layer includes the deterministic `PoemBackend` and an isolated, read-only
`CodexExecBackend`, plus an `OmlxBackend` for local OpenAI-compatible oMLX
servers.

The next prototype composes attempts through the policy-controlled
[`ChildDispatcher`](harness_labs/composition.py). A parent submits an
authority-free `ChildRequest` containing a role, objective, and task-specific
context string. The dispatcher copies that string unchanged to the child
attempt, chooses fixed task, grant, backend-configuration, and executor
references, enforces depth and child-count limits, invokes the existing
`AttemptRunner`, and records parent/child events.
Provider integration has one narrow [`AgentSession`](harness_labs/agent_sessions.py)
contract and one controller-owned tool loop. The resident Codex app-server
session exposes only the controller's dynamic child tool and stays alive while
the child works. The oMLX session translates the same logical tool exchange into
two structured text completions because its adapter does not use a native tool
transport.

The analysis-and-planning prototype now includes a
[`ControllerKernel`](harness_labs/controller_kernel.py) surrounded by a resident
[`CoordinatorLoop`](harness_labs/controller_coordinator.py). Models submit a
fixed command language; the kernel owns revisions, idempotency, tasks, bounds,
criteria, findings, completion, checkpoints, and audit receipts. A generic
capability scheduler creates a fresh executor for every parallel attempt, so the
coordinator may choose repeated roles and bounded subchildren. The coordinator
sees a compact deterministic `RunView` and opens full artifacts only by
reference. See the
[`hybrid controller status`](docs/development/hybrid-controller-coordinator.md)
and [architectural decision](docs/decisions/0004-hybrid-controller-command-kernel.md).

A schema-defined dispatcher replaces coordinators at declared context
boundaries while the kernel remains authoritative. `run_feature_worktree(...)`
creates an isolated branch/worktree, executes the schema, runs its declared
verification command with bounded same-worktree repair on failure, optionally
runs the ledger-backed review/fix loop, commits only declared paths, and either
leaves a merge-ready candidate or performs a guarded merge.

The shipped PlanGraph CLI admits a committed canonical `plan-graph-plan/1`
decomposition only with an operator-attested approval receipt. Use
`scripts/approve_plan.py prepare` to freeze and gate the subject, record an
operator attestation using `schemas/plan-operator-approval.schema.json`, then
use `scripts/approve_plan.py issue` and pass the resulting receipt to
`scripts/run_plan_graph.py run --approval-receipt` (registration of the
approved decomposition happens under the receipt; legacy decompositions still
use `register` plus `run --registration`). The receipt binds the exact Git
revision, scope grants, verification commands and timeouts, repository identity,
and local executable evidence.

The portable implement-v13 policy adds source binding, FRAME/NECESSITY/MECHANISM
plan refutation, build-handoff, and risk-shaped review obligations. Required
segment exit artifacts make them deterministic gates rather than prompt-only
requests. Browser, network, and external-effect access shares a deny-by-default
capability broker with target allowlists, authorization, idempotency, injected
handlers, and durable receipts. Finalized runs write `summary.json` with
duration, tokens, cache reads, tool calls, and explicitly sourced costs.

A complete deterministic poem attempt is available in
[`examples/run_poem_attempt.py`](examples/run_poem_attempt.py). It remains a
small executor example rather than a production FeatureRun.

Run the example from the repository root:

```sh
python3 -m examples.run_poem_attempt
```

Compare the same task, context, and grant across both backends:

```sh
python3 -m examples.compare_poem_backends
```

Run the same task on `Qwen3.5-4B-MLX-4bit` after starting oMLX on the loopback
endpoint `http://127.0.0.1:8100/v1` with
`~/.lmstudio/models` as its model directory:

```sh
python3 -m examples.run_omlx_poem_attempt
```

Run the treasure test with a resident Codex parent and a file-reading Codex
child:

```sh
python3 -m examples.run_delegated_treasure_attempt --backend codex
```

Start the Retinology oMLX server, then compare both backends on the identical
attempt:

```sh
/Users/kirillzaslavsky/claudeprojects/RDPcrawler/.omlx-venv/bin/python \
  /Users/kirillzaslavsky/claudeprojects/RDPcrawler/scripts/start_omlx_server.py \
  --port 8100 --max-memory 8GB
python3 -m examples.run_delegated_treasure_attempt --backend all
```

Parent and child backends are selected independently. Exercise all four routes:

```sh
python3 -m examples.run_delegated_treasure_attempt --parent all --child all
```

Every route dispatches exactly one child. The parent receives the path to
`treasure_locator.txt` as context and passes locator instructions to the child;
the target path exists only inside that locator file. A Codex child has
`read_file`, follows the locator, and returns `there is booty here` with
file-read evidence. An oMLX child lacks that capability, but Qwen still runs and
must return exactly
`sorry, I cannot do that, Dave.` The result records model-invocation and
capability-unavailable evidence.

Each treasure route creates a private, durable audit directory under
`logs/runs/` and prints its independently anchorable chain-head hash. The
journal captures controller authorization, parent/child causality, exact
backend transport, session identities, command receipts, durations, final
results, and termination proof. Validate it with:

```sh
python3 -m scripts.audit_run verify logs/runs/<run-id>
```

The scenario enables retained child sessions. After the initial response, the
parent sends the same child `what enabled you to answer me this way?`, records
the model's explanation, and the controller terminates the child handle. Output
must report one child, two child responses, and successful termination.

Run the inherited contract suite with Python 3.11 or later:

```sh
python3 scripts/check_repository_contracts.py
python3 -m unittest discover -s tests -v
```

The first implementation milestone is defined in
[`docs/development/NEXT_STEPS.md`](docs/development/NEXT_STEPS.md).

Run the live hybrid-controller flexibility suite against an explicitly selected
clean repository worktree:

```sh
python3 -m harness_labs.controller_live_scenarios \
  --repository /absolute/path/to/repository-worktree \
  --scenario all
```

The live runner uses one resident, tool-only Codex coordinator, fresh read-only
Codex semantic workers, hashed evidence artifacts, and controller-owned fixed
commands for capabilities such as an isolated Playwright verification walk.
