# Hybrid controller and coordinator

Status: implemented prototype
Date: 2026-08-03

## Implemented slice

The repository now has a provider-neutral hybrid controller for analysis and
planning runs:

```text
operator objective
  -> deterministic kernel and RunView
  -> resident coordinator AgentSession
  -> typed command or evidence query
  -> capability scheduler
  -> fresh TaskAttempt executor
  -> semantic result and content-addressed evidence
  -> kernel gates and durable checkpoint
```

The kernel accepts versioned commands carrying actor, expected revision,
idempotency key, provenance, and payload. It owns task registration, bounds,
results, criteria, decisions, finding dispositions, completion, and audit state.
The coordinator stays resident while child batches execute and receives updated
projections in tool results.

The scheduler supports repeated role profiles because each attempt receives a
fresh executor. Required capabilities are matched before any task in a batch
starts. A schema-valid worker result may request subchildren when its task grant
allows delegation; the same run-level depth, fan-out, task, and concurrency
limits apply.

## Flexibility suite

The same controller runs:

1. ten-commit research, gap decision, plan, two parallel adversarial reviews,
   finding disposition, revision, and final plan;
2. a coordinator-selected parallel UI-inspection batch, evidence-preserving
   deduplication, diagnosis, and proposed fixes; and
3. architecture/functionality/UI appraisal, a delegated architecture subchild,
   ideal-product synthesis, gap analysis, and website proposal.

Scenario-specific concepts exist only in run/task contracts and result details.
The kernel, projection, command, and coordinator modules contain no scenario
switch.

## Runtime and recovery

`python3 -m harness_labs.controller_run --fixture SPEC --run-dir NEW_DIR` runs a
deterministic fixture through the real kernel, scheduler, coordinator loop, audit
journal, checkpoint, and manifest.

`resume_controller(...)` reconstructs the kernel from the durable checkpoint and
controller events, restores content-addressed evidence, and opens a new
coordinator session. Accepted command receipts survive restart. A repeated
dispatch runs still-ready reserved work once and does not rerun completed work.

## Live verification

`python3 -m harness_labs.controller_live_scenarios` binds the resident
coordinator to `CodexAppServerSession` and role profiles to fresh
`CodexSemanticTaskExecutor` processes. Workers return constrained raw JSON; the
adapter registers the prose deliverable and any controller-owned command receipt
in the evidence catalog, injects their real hashes, constructs the semantic
result envelope, and validates it before the kernel sees it.

Capabilities that require writable transient state, such as a Playwright walk,
run as a fixed controller-owned command in the explicitly selected repository
worktree. The reasoning worker remains read-only and receives the complete
command receipt as context. This separates deterministic execution authority
from model interpretation without pretending that a read-only model sandbox can
launch a stateful browser harness.

The live flexibility suite exercises commit planning/review, parallel UI
diagnosis with a real browser gate, and a broad product appraisal with parallel
terminal branches. Their append-only runs live under `logs/runs/`.

The first recorded live verification against Retinology commit
`a1d454c4f31ddcf931875e03ab98b89da69b1e1e` produced:

- `20260804T025559Z-history-plan`: succeeded; five tasks, two parallel
  adversarial reviewers, exactly ten surveyed commits, and a revised final plan;
- `20260804T030519Z-dark-mode-ui`: blocked honestly because a browser command
  attempted to run inside the read-only model sandbox and at the wrong checkout;
- `20260804T031418Z-dark-mode-ui`: succeeded after moving the fixed browser
  command to a controller-owned capability adapter; the Playwright gate passed
  all 16 live checks and 17 Chromium steps; and
- `20260804T032228Z-idealized-product`: succeeded; three parallel domain
  appraisals, current/ideal synthesis, then parallel gap-analysis and website
  proposal branches.

All four event chains and every registered artifact digest were independently
verified. The blocked run remains part of the evidence rather than being
overwritten.

## Remaining production work

- Bind local-model coordinator and semantic worker configurations through the
  same supported CLI.
- Generalize controller-owned capability commands into a policy-controlled
  registry rather than profile-local fixed commands.
- Add native screenshot/image evidence ingestion; the current browser receipt
  proves the repository browser gate and computed-style assertions, but does not
  make screenshots first-class controller artifacts.
- Add writable-path isolation, repository snapshots, worktree creation, and Git
  integration transactions.
- Expand budgets from structural task/concurrency limits to measured token,
  runtime, and monetary consumption.
- Add operator pause/resume and approval surfaces above the existing typed kernel
  commands.
- Incorporate this controller into the full
  `orient -> plan -> implement -> verify -> review -> integrate -> report`
  feature lifecycle.

## Verification

```sh
python3 scripts/check_repository_contracts.py
python3 -m unittest discover -s tests -v
```

The focused controller tests also live in `tests/test_controller_*.py`.
