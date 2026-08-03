# 0002 — Pass-through child context

Status: accepted
Date: 2026-08-03
Owners: harness controller

## Context

Child dispatch selected a role and objective, while every role authorization
fixed one `context_ref`. A parent therefore could not give two attempts of the
same role different task-specific starting information without creating new
controller configuration. Designing selection, authorization, resolution, and
packet optimization together would make the first compositional contract much
larger than necessary.

## Decision

`ChildRequest` has one additional field, `context: str`. The controller copies
that string unchanged onto the immutable child `TaskAttempt`. Executors receive
the attempt and may place the supplied string in their backend prompt or model
context.

The controller currently performs only structural type validation. It does not
select, rewrite, authorize, summarize, resolve, or enrich the context. Existing
role, capability, depth, fan-out, backend, and workspace controls remain
separate and continue to apply.

The exact string is retained in the child-dispatch audit artifact and its
SHA-256 digest is present in the structured event. Empty context is valid for
direct controller calls, while model-facing spawn tool schemas require an
explicit string so omission is visible.

## Alternatives

- Keep context fixed in `ChildAuthorization`. This prevents task-specific
  parent-to-child handoff.
- Add a context selector, policy engine, packet store, and materializer now.
  Those mechanisms are useful for production isolation but are not required to
  prove composition.
- Put supplied text into a mutable shared reference store. That adds lifecycle
  and concurrency semantics merely to pass one immutable value.

## Evidence

- `tests/test_composition.py` proves byte-for-byte propagation from
  `ChildRequest` to `TaskAttempt` and digest binding.
- `tests/test_agent_sessions.py` proves native, emulated, individual, retained,
  and batch tool transports carry the field.
- `tests/test_codex_delegation.py` proves a Codex child uses supplied text to
  find a locator file, reads the hidden target path from that file, and returns
  the target contents.

## Consequences

The primitive is easy to compose at any hierarchy depth and does not require an
LLM-based assembler. It is not a production context-security boundary: a parent
can place any text it knows into the field. Later policy can inspect or replace
this pass-through at the controller boundary without changing the executor
result contract.

## Validation and reversal

Keep the direct string while tests show identical behavior across backend
transports and real delegation. Supersede this decision when measured context
size, redaction, provenance, or privilege-isolation requirements justify a
versioned context-packet reference.
