# Development Index

Status: active

## Current direction

- Contract-burden reduction (living): [`contract-burden-reduction.md`](contract-burden-reduction.md)

- Next milestone: [`NEXT_STEPS.md`](NEXT_STEPS.md)
- Harness contract: [`../architecture/harness-contract.md`](../architecture/harness-contract.md)
- Context contract: [`../architecture/context-engineering.md`](../architecture/context-engineering.md)
- Coordinator dispatch contract:
  [`../architecture/coordinator-dispatch.md`](../architecture/coordinator-dispatch.md)
- Portable development policy:
  [`../architecture/development-policy.md`](../architecture/development-policy.md)
- Capability brokers:
  [`../architecture/capability-brokers.md`](../architecture/capability-brokers.md)
- Observability contract: [`../observability/logging-and-metrics.md`](../observability/logging-and-metrics.md)
- Durable decisions: [`../decisions/README.md`](../decisions/README.md)
- Execution-first lifecycle decision:
  [`../decisions/0001-execution-first-production-lifecycle.md`](../decisions/0001-execution-first-production-lifecycle.md)
- Active implement-v13 efficiency repair:
  [`implement-v13-efficiency-repair-plan.md`](implement-v13-efficiency-repair-plan.md)
- Proposed platform-agnostic autonomous workflow:
  [`platform-agnostic-autonomous-workflow-strategy.md`](platform-agnostic-autonomous-workflow-strategy.md)
- Initial runtime primitive:
  [`../../harness_labs/attempts.py`](../../harness_labs/attempts.py)
- Parallel child dispatch:
  [`parallel-child-dispatch-plan.md`](parallel-child-dispatch-plan.md)
- Parallel PlanGraph execution and Retinology replay:
  [`parallel-plangraph-execution-replay-plan.md`](parallel-plangraph-execution-replay-plan.md)
- Parallel PlanGraph admission and recovery contract:
  [`../decisions/0006-parallel-plangraph-contract.md`](../decisions/0006-parallel-plangraph-contract.md)
- Pass-through child context:
  [`pass-through-child-context-plan.md`](pass-through-child-context-plan.md)
- Hybrid controller and coordinator:
  [`hybrid-controller-coordinator.md`](hybrid-controller-coordinator.md)
- Claude `solo-phased-reviewfix` and FeatureRun comparison:
  [`solo-phased-reviewfix-feature-run-comparison.md`](solo-phased-reviewfix-feature-run-comparison.md)
- Live React Flow PlanGraph and FeatureRun dashboard:
  [`live-plangraph-dashboard-plan.md`](live-plangraph-dashboard-plan.md)
- Live dashboard startup, liveness, and legacy migration:
  [`live-plangraph-dashboard-operations.md`](live-plangraph-dashboard-operations.md)
- PlanGraph and FeatureRun projection design:
  [`plan-projection-design.md`](plan-projection-design.md)
- Repository-bound PlanGraph approval design:
  [`plan-approval-design.md`](plan-approval-design.md)
- Platform-patch merge audit for the Retinology Flow-Editor campaign (P1-P12):
  [`HARNESS_LABS_PATCH_AUDIT_20260818.md`](HARNESS_LABS_PATCH_AUDIT_20260818.md)
- Porting `dashboard_improve` to main, survey and decision:
  [`dashboard-improve-port-plan.md`](dashboard-improve-port-plan.md)
- Session handoff and ranked backlog (2026-08-19):
  [`SESSION_HANDOFF_20260819.md`](SESSION_HANDOFF_20260819.md)
- In-graph escalation of out-of-scope findings, with bounded unsealing
  (ADR 0007): [`in-graph-escalation-unsealing-plan.md`](in-graph-escalation-unsealing-plan.md),
  its two-node decomposition
  [`cc08-escalation-decomposition.json`](cc08-escalation-decomposition.json),
  and the implementer handoff
  [`SESSION_HANDOFF_CC08_ESCALATION_20260819.md`](SESSION_HANDOFF_CC08_ESCALATION_20260819.md)

## Seed implementation records

Files in this directory named `current_implementation_*`,
`plan_review_log.md`, and `serial_implementation_decisions.jsonl` are historical
records from the inherited **Initializing** bootstrap package dated 2026-07-14.
They describe that utility, not the current Harness Labs milestone.

New feature runs should write runtime state under `logs/runs/<run-id>/` and keep
human-facing plans in this directory with feature-specific names.
