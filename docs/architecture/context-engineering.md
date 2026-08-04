# Context Engineering Contract

Status: normative

Context is an input artifact, not an informal transcript. Every dispatched task
MUST receive a versioned context packet that is sufficient, minimal, traceable,
and explicit about uncertainty.

The current prototype composition layer has a deliberately smaller transport:
the parent supplies one `ChildRequest.context` string and the controller copies
it unchanged to the child attempt. That string is bootstrap context, not yet the
production packet required by this contract. Role capabilities and workspace
authority remain separate. A future packet boundary may validate or replace the
string without changing the parent/child result envelope.

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
8. Inject immutable execution-environment facts into every command-capable role.
   For a macOS/zsh run this includes BSD userland, zsh read-only parameters,
   portable file discovery, and the distinction between an optional search's
   no-match exit and a required assertion failure. Do not rely on the coordinator
   to remember to copy these facts into child prompts.

## Parent/child boundary

A parent may summarize broader context but remains accountable for omissions.
A child may gather additional context through its separately granted tools;
supplied bootstrap context does not restrict those capabilities.
A child MUST report missing or contradictory context instead of silently
inventing requirements. Child output returns through the result contract; it
does not become trusted shared context until validated and promoted by the
parent.

## Production-consumer trace

Every context packet MUST name the production role and executable dispatch path
that consumes it. Context infrastructure without a launched production consumer
is not lifecycle progress and MUST NOT be counted as feature completion.

Each packet source MUST trace to an acceptance criterion or a decision assigned
to that role. Proposed context expansion without that trace MUST be deferred. An
end-to-end lifecycle test MUST exercise packet assembly through the same production
entrypoint used by a real run; prompt-unit and synthetic-context tests are only
supporting evidence.

## Context quality metrics

Track at least packet size, source count, stale-reference failures, clarification
requests, missing-context retries, irrelevant-context findings, and result
accuracy. Evaluate context strategies against the same task suite and model/tool
configuration before attributing improvement to the strategy.
