# Decision Records

Status: active

Use this directory for durable decisions that change Harness Labs architecture,
contracts, evaluation, safety boundaries, or integration policy. Run-local
choices begin in `logs/runs/<run-id>/decisions.jsonl`; promote only decisions
that future contributors must understand.

Name records `NNNN-short-title.md` and use [`TEMPLATE.md`](TEMPLATE.md). A record
is immutable after acceptance except for status and links to superseding records.

## Accepted decisions

- [`0001 — Execution-first production lifecycle`](0001-execution-first-production-lifecycle.md)
- [`0002 — Controller-owned parallel child batches`](0002-controller-owned-parallel-child-batches.md)
- [`0003 — Pass-through child context`](0003-pass-through-child-context.md)
- [`0004 — Hybrid controller command kernel`](0004-hybrid-controller-command-kernel.md)
- [`0005 — Ledger-backed review/fix gate`](0005-ledger-backed-review-fix.md)
- [`0006 — Repository-bound PlanGraph approval`](0006-repository-bound-plan-approval.md)
