# Decision Records

Status: active

Use this directory for durable decisions that change Harness Labs architecture,
contracts, evaluation, safety boundaries, or integration policy. Run-local
choices begin in `logs/runs/<run-id>/decisions.jsonl`; promote only decisions
that future contributors must understand.

Name records `NNNN-short-title.md` and use [`TEMPLATE.md`](TEMPLATE.md). A record
is immutable after acceptance except for status and links to superseding records.

Accepted records:

- [`0002-controller-owned-parallel-child-batches.md`](0002-controller-owned-parallel-child-batches.md)
- [`0003-pass-through-child-context.md`](0003-pass-through-child-context.md)
