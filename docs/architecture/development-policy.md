# Portable Development Policy

Status: implemented

`DevelopmentPolicy` carries feature-development quality rules independently of
the model backend. The initial `implement-v13-sourcebound-riskreview/1` policy
requires source-bound planning claims, dependency-ordered steps, runtime
contracts, FRAME/NECESSITY/MECHANISM refutation, two independent plan-review
rounds, a curated build briefing, and review assignments derived from changed
paths.

The coordinator schema embeds the complete versioned policy and binds it into
the schema SHA-256. The dispatcher supplies it to each relevant fresh
coordinator. `exit_artifact_kinds` prevent a segment from crossing its boundary
until its declared planning, implementation, verification, review, or report
evidence exists.

The policy guides semantic decomposition. The deterministic kernel and
dispatcher continue to own authority, evidence registration, phase transitions,
and stopping. `standard_feature_run_dispatch_schema()` is the reference seven-phase
schema.
