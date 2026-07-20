# Context Engineering Contract

Status: normative

Context is an input artifact, not an informal transcript. Every dispatched task
MUST receive a versioned context packet that is sufficient, minimal, traceable,
and explicit about uncertainty.

## Required packet fields

A context packet records:

- task, parent task, actor role, and objective;
- acceptance criteria and relevant quality gates;
- allowed scope, writable paths, permissions, and prohibited actions;
- repository revision, base revision, and relevant file or symbol references;
- applicable instruction sources in precedence order;
- upstream decisions and dependency results;
- required output schema and evidence format;
- token or size budget, deliberate exclusions, and escalation triggers;
- a content hash or immutable references for reproducibility.

## Assembly rules

1. Start from the task contract and add only evidence needed to make or verify
   the assigned decision.
2. Prefer precise file ranges, symbols, structured summaries, and artifact
   references over whole-repository dumps or accumulated chat history.
3. Separate observed facts, external source claims, and inferences. Preserve
   provenance and freshness for time-sensitive inputs.
4. Resolve conflicting instructions before dispatch. Never ask a child agent to
   infer precedence from multiple contradictory documents.
5. Redact secrets and sensitive user data before persistence or dispatch.
6. Refresh a packet when its referenced branch, commit, contract, or dependency
   result changes materially.
7. Record context size and composition so later experiments can measure which
   inputs improved correctness or merely added cost.

## Parent/child boundary

A parent may summarize broader context but remains accountable for omissions.
A child MUST report missing or contradictory context instead of silently
inventing requirements. Child output returns through the result contract; it
does not become trusted shared context until validated and promoted by the
parent.

## Context quality metrics

Track at least packet size, source count, stale-reference failures, clarification
requests, missing-context retries, irrelevant-context findings, and result
accuracy. Evaluate context strategies against the same task suite and model/tool
configuration before attributing improvement to the strategy.
