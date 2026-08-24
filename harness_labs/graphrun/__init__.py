"""GraphRun: FeatureRun execution composed under PlanGraph orchestration,
plus the operator-facing composition surface.

May import every other layer; nothing imports graphrun
(GRAPHRUN_RESTRUCTURE_PLAN.md rule 5). Program runners live in
``experiments/`` and consume this surface.
"""

from harness_labs.featurerun.feature_run import (
    PlanGraphFeatureRunBinding,
    run_feature_worktree,
    run_plan_graph_feature_worktree,
)
from harness_labs.graphrun.agent_mixture import (
    BackendSpec,
    UI_PLAYWRIGHT_CAPABILITY,
    UI_PLAYWRIGHT_ROLE,
    WorkerRole,
    build_role_profiles,
    parse_backend_spec,
)
from harness_labs.plangraph.plan_approval import (
    PlanApprovalAdmission,
    issue_receipt,
    prepare_approval,
)
from harness_labs.plangraph.plan_graph import (
    PlanGraph,
    RepairResumeDirective,
)

__all__ = [
    "BackendSpec",
    "PlanApprovalAdmission",
    "PlanGraph",
    "PlanGraphFeatureRunBinding",
    "RepairResumeDirective",
    "UI_PLAYWRIGHT_CAPABILITY",
    "UI_PLAYWRIGHT_ROLE",
    "WorkerRole",
    "build_role_profiles",
    "issue_receipt",
    "parse_backend_spec",
    "prepare_approval",
    "run_feature_worktree",
    "run_plan_graph_feature_worktree",
]
