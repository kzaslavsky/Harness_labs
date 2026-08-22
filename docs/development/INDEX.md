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
- Dashboard observability metrics plan (graph rollup, naming, snapshot
  contract, server API, live and completed-viewer UI, historical
  reconstruction):
  [`DASHBOARD_OBSERVABILITY_METRICS_PLAN.md`](DASHBOARD_OBSERVABILITY_METRICS_PLAN.md)
- Completed-PlanGraph viewer backfill and viewing runbook:
  [`../observability/completed-plangraph-viewer.md`](../observability/completed-plangraph-viewer.md)
- Delta-to-run pipeline (finding intake, sanitizer media-type policy,
  measurer commissioning, launcher kit and plan synthesis):
  [`delta-to-run-plan.md`](delta-to-run-plan.md), its five-node decomposition
  [`delta-to-run-decomposition.json`](delta-to-run-decomposition.json), the
  agent guide [`delta-to-run-agent-guide.md`](delta-to-run-agent-guide.md), and
  the session handoff
  [`SESSION_HANDOFF_DELTA_TO_RUN_20260820.md`](SESSION_HANDOFF_DELTA_TO_RUN_20260820.md)
- Engineering-memory port (impact analysis, finding history, decision
  lifecycle): [`engineering-memory-port-plan.md`](engineering-memory-port-plan.md)
  and its six-node decomposition
  [`engineering-memory-decomposition.json`](engineering-memory-decomposition.json).
  New modules: static-import impact analysis
  [`../../harness_labs/plangraph/impact_analysis.py`](../../harness_labs/plangraph/impact_analysis.py),
  repo-scoped finding history
  [`../../harness_labs/plangraph/finding_history.py`](../../harness_labs/plangraph/finding_history.py)
  (folding [`../../harness_labs/plangraph/convergence_ledger.py`](../../harness_labs/plangraph/convergence_ledger.py)'s
  public per-key lineage accessor), and the decision registry
  [`../../harness_labs/core/decision_registry.py`](../../harness_labs/core/decision_registry.py).
  Agent how-to (start here):
  [`engineering-memory-agent-guide.md`](engineering-memory-agent-guide.md)
  Wired into admission
  ([`../../harness_labs/plangraph/plan_approval.py`](../../harness_labs/plangraph/plan_approval.py):
  `REQUIRED_PATHS_IMPACT_WARNING`, `gates["notices"]`), refinement advisories
  and the campaign driver's history-aware ingest
  ([`../../harness_labs/plangraph/plan_refinement.py`](../../harness_labs/plangraph/plan_refinement.py),
  [`../../scripts/run_convergence_campaign.py`](../../scripts/run_convergence_campaign.py)).
- Self-improvement agent (scheduled EM audit + delta-to-run planning +
  convergence close, one braid over the three systems above, no parallel
  machinery): [`self-improvement-agent-plan.md`](self-improvement-agent-plan.md)
  and its registerable decomposition
  [`self-improvement-decomposition.json`](self-improvement-decomposition.json).
  Agent how-to (start here):
  [`self-improvement-agent-guide.md`](self-improvement-agent-guide.md).
  Recurrence: the repo-owned entry point `scripts/self_improve.py audit
  --propose-if-ready`, scheduled locally via the launchd template
  [`../operations/self-improve.launchd.plist.example`](../operations/self-improve.launchd.plist.example)
  (CI cannot mine — `logs/runs/*` is gitignored and local). Committed
  artifact home: [`../improvement/`](../improvement/), validated by
  [`../../scripts/dev/check_improvement_artifacts.py`](../../scripts/dev/check_improvement_artifacts.py).
  Close-out promotion drafts a `docs/decisions/` record from
  `docs/decisions/TEMPLATE.md`
  ([`../../harness_labs/graphrun/improvement_loop.py`](../../harness_labs/graphrun/improvement_loop.py):
  `draft_decision_record`) for an operator to land by hand.

## Seed implementation records

Files in this directory named `current_implementation_*`,
`plan_review_log.md`, and `serial_implementation_decisions.jsonl` are historical
records from the inherited **Initializing** bootstrap package dated 2026-07-14.
They describe that utility, not the current Harness Labs milestone.

New feature runs should write runtime state under `logs/runs/<run-id>/` and keep
human-facing plans in this directory with feature-specific names.
