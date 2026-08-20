# Decision Records

Status: active

Use this directory for durable decisions that change Harness Labs architecture,
contracts, evaluation, safety boundaries, or integration policy. Run-local
choices begin in `logs/runs/<run-id>/decisions.jsonl`; promote only decisions
that future contributors must understand.

Name records `NNNN-short-title.md` and use [`TEMPLATE.md`](TEMPLATE.md). A record
is immutable after acceptance except for status and links to superseding records.
An accepted record's header block (the lines before its first `## ` heading) may
also gain new optional header lines — such as `Concerns-paths:` — without that
counting as a body change, provided the body from the first `## ` heading onward
stays byte-identical; this is a header-only amendment, distinct from a
supersession. ADR 0007's 2026-08-20 amendment is body prose within that single
record rather than a supersession, and the two files numbered `0006` are a
numbering collision between distinct active decisions rather than one
superseding the other.

## Accepted decisions

- [`0001 — Execution-first production lifecycle`](0001-execution-first-production-lifecycle.md)
- [`0002 — Controller-owned parallel child batches`](0002-controller-owned-parallel-child-batches.md)
- [`0003 — Pass-through child context`](0003-pass-through-child-context.md)
- [`0004 — Hybrid controller command kernel`](0004-hybrid-controller-command-kernel.md)
- [`0005 — Ledger-backed review/fix gate`](0005-ledger-backed-review-fix.md)
- [`0006 — Parallel PlanGraph admission and recovery contract`](0006-parallel-plangraph-contract.md)
- [`0006 — Repository-bound PlanGraph approval`](0006-repository-bound-plan-approval.md)
- [`0007 — In-graph escalation with bounded unsealing`](0007-in-graph-escalation-bounded-unsealing.md)
