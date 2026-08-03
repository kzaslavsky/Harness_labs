# Development Index

Status: active

## Current direction

- Next milestone: [`NEXT_STEPS.md`](NEXT_STEPS.md)
- Harness contract: [`../architecture/harness-contract.md`](../architecture/harness-contract.md)
- Context contract: [`../architecture/context-engineering.md`](../architecture/context-engineering.md)
- Observability contract: [`../observability/logging-and-metrics.md`](../observability/logging-and-metrics.md)
- Durable decisions: [`../decisions/README.md`](../decisions/README.md)
- Parallel child dispatch:
  [`parallel-child-dispatch-plan.md`](parallel-child-dispatch-plan.md)
- Pass-through child context:
  [`pass-through-child-context-plan.md`](pass-through-child-context-plan.md)

## Seed implementation records

Files in this directory named `current_implementation_*`,
`plan_review_log.md`, and `serial_implementation_decisions.jsonl` are historical
records from the inherited **Initializing** bootstrap package dated 2026-07-14.
They describe that utility, not the current Harness Labs milestone.

New feature runs should write runtime state under `logs/runs/<run-id>/` and keep
human-facing plans in this directory with feature-specific names.
