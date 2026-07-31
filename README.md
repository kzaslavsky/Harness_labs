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

The repository was seeded with a dependency-free project initializer. Its
launcher, templates, assets, portable skills, and contract tests remain under
`bin/`, `templates/`, `assets/`, `skills/`, and `tests/`. They are the initial
bootstrap utility, not the complete Harness Labs runtime.

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
authority-free `ChildRequest`; the dispatcher chooses fixed child task, context,
grant, and executor references, enforces depth and child-count limits, invokes
the existing `AttemptRunner` recursively, and records parent/child events.
Provider integration has one narrow [`AgentSession`](harness_labs/agent_sessions.py)
contract and one controller-owned tool loop. The resident Codex app-server
session exposes only the controller's dynamic child tool and stays alive while
the child works. The oMLX session translates the same logical tool exchange into
two structured text completions because its adapter does not use a native tool
transport.

A complete deterministic poem attempt is available in
[`examples/run_poem_attempt.py`](examples/run_poem_attempt.py). Scheduling,
persistence, external capability enforcement, and lifecycle control remain future
vertical slices.

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

Every route dispatches exactly one child. A Codex child has `read_file` and
returns `there is booty here` with file-read evidence. An oMLX child lacks that
capability, but Qwen still runs and must return exactly
`sorry, I cannot do that, Dave.` The result records model-invocation and
capability-unavailable evidence.

Run the inherited contract suite with Python 3.11 or later:

```sh
python3 scripts/check_repository_contracts.py
python3 -m unittest discover -s tests -v
```

The first implementation milestone is defined in
[`docs/development/NEXT_STEPS.md`](docs/development/NEXT_STEPS.md).
