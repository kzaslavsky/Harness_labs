# 0001 — Campaign launcher timeout floor

Status: accepted
Concerns-paths: harness_labs/graphrun/campaign_launcher.py
Date: 2026-05-01
Owners: Harness Labs maintainers
Run: not applicable

## Context

A fixture governing decision for the improvement-program tests
(`tests/test_improvement_program.py`): it exists only to give
`decision_registry.load_decisions` a real accepted ADR whose
`Concerns-paths` covers `harness_labs/graphrun/campaign_launcher.py`, so the
drafter's uncited-governed-path refusal has a decision id to require and
cite.

## Decision

Retry-budget timeouts in the campaign launcher never fall below the floor
established here.

## Validation and reversal

Fixture only; not a real repository decision.
