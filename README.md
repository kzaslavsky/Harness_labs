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
Scheduling, persistence, capability enforcement, and lifecycle control remain
future vertical slices.

Run the inherited contract suite with Python 3.11 or later:

```sh
python3 scripts/check_repository_contracts.py
python3 -m unittest discover -s tests -v
```

The first implementation milestone is defined in
[`docs/development/NEXT_STEPS.md`](docs/development/NEXT_STEPS.md).
