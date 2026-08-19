"""End-to-end FeatureRun Git transaction tests."""

from __future__ import annotations

import hashlib
import json
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, RunContract
from harness_labs.core.controller_results import semantic_payload
from harness_labs.core.controller_scheduler import RoleProfile
from harness_labs.featurerun.feature_run import (
    DeterministicVerificationResult,
    FeatureRunHandoffArtifact,
    FeatureRunResult,
    PlanGraphFeatureRunBinding,
    RecoveryContext,
    RecoveryDecision,
    ReviewFixPolicy,
    ReviewFixResult,
    run_feature_worktree,
    run_plan_graph_feature_worktree,
    classify_verification_failure,
)
from harness_labs.featurerun.review_fix import ReviewFixLoop, ReviewLedger
from harness_labs.featurerun.feature_run_policy import (
    standard_feature_run_dispatch_schema,
    standard_composed_recovery_agent,
    standard_review_continuation_recovery_agent,
)
from harness_labs.core.coordinator_schema import (
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)
from tests.controller_scenario_fixtures import ScriptedCoordinatorSession


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


class _BuildExecutor:
    def __init__(self, task, worktree, evidence) -> None:
        self.task = task
        self.worktree = worktree
        self.evidence = evidence

    def execute(self, attempt) -> TaskResult:
        (self.worktree / "feature.txt").write_text("built\n", encoding="utf-8")
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built feature.txt\n",
            media_type="text/markdown",
            producer_task_id=self.task["id"],
        )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built.",
                details_schema=self.task["details_schema"],
                details={"paths": ["feature.txt"]},
                artifacts=(artifact.as_dict(),),
                criterion_coverage=(
                    {
                        "criterion_id": "built",
                        "status": "satisfied",
                        "evidence_refs": [artifact.ref],
                    },
                ),
            ),
            evidence=(artifact.ref,),
        )


class _ReviewRepairFactory:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.review_count = 0

    def __call__(self, stage, attempt):
        factory = self

        class Executor:
            def execute(self, current_attempt) -> TaskResult:
                if stage == "review":
                    factory.review_count += 1
                    findings = (
                        (
                            {
                                "id": "review-fix",
                                "file": "feature.txt",
                                "subject": "review fix",
                                "statement": "Record the reviewed candidate.",
                                "category": "correctness",
                                "severity": "major",
                                "score": 90,
                                "fix_cost": "local",
                                "protects": "acceptance criterion built",
                                "requires_disposition": True,
                            },
                        )
                        if factory.review_count == 1
                        else ()
                    )
                    return TaskResult(
                        current_attempt.attempt_id,
                        "succeeded",
                        semantic_payload(
                            summary="Review complete.",
                            details_schema="review-fix-review/1",
                            details={},
                            findings=findings,
                        ),
                    )
                key = "feature.txt:review-fix"
                if stage == "fix":
                    (factory.worktree / "reviewed.txt").write_text(
                        "reviewed\n", encoding="utf-8"
                    )
                    details = {"addressed_finding_keys": [key]}
                else:
                    details = {"verified_finding_keys": [key]}
                return TaskResult(
                    current_attempt.attempt_id,
                    "succeeded",
                    semantic_payload(
                        summary=f"{stage} complete.",
                        details_schema=f"review-fix-{stage}/1",
                        details=details,
                    ),
                )

        return Executor()


class _VerificationRepairExecutor:
    def __init__(self, worktree, seen_contexts, *, repair=True) -> None:
        self.worktree = worktree
        self.seen_contexts = seen_contexts
        self.repair = repair

    def execute(self, attempt) -> TaskResult:
        self.seen_contexts.append(json.loads(attempt.context))
        if self.repair:
            (self.worktree / "verified.txt").write_text(
                "repaired\n",
                encoding="utf-8",
            )
        return TaskResult(
            attempt.attempt_id,
            "succeeded",
            {"summary": "Applied bounded verification repair."},
        )


class _VerificationRecoveryAgent:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.contexts = []

    def __call__(self, context):
        self.contexts.append(context)
        (self.worktree / "verified.txt").write_text(
            "recovered\n",
            encoding="utf-8",
        )
        return RecoveryDecision(
            "adjust_plan",
            "Verification showed that the plan omitted the recovery marker.",
            {"add_step": "Create and verify the recovery marker."},
        )


class _FailOnceReviewFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, stage, attempt):
        factory = self

        class Executor:
            def execute(self, current_attempt):
                factory.calls += 1
                if factory.calls == 1:
                    return TaskResult(
                        current_attempt.attempt_id,
                        "failed",
                        {"error": "transient reviewer failure"},
                    )
                return TaskResult(
                    current_attempt.attempt_id,
                    "succeeded",
                    semantic_payload(
                        summary="Review cleared after recovery.",
                        details_schema="review-fix-review/1",
                        details={},
                        findings=(),
                    ),
                )

        return Executor()


class _ContinuationReviewFactory:
    """Review that exhausts a one-cycle limit, then clears once continued.

    Stages are keyed off the cycle ordinal in the attempt id, so a loop that
    restarted at cycle one instead of continuing would ask for a script that
    was already consumed.
    """

    _FINDING = {
        "id": "guard",
        "statement": "The guard is missing.",
        "category": "correctness",
        "severity": "major",
        "requires_disposition": True,
        "file": "feature.txt",
        "subject": "missing guard",
        "score": 90,
        "fix_cost": "local",
        "protects": "acceptance criterion built",
    }
    KEY = "feature.txt:missing-guard"

    def __init__(self) -> None:
        self.attempt_ids: list[str] = []

    def __call__(self, stage, attempt):
        factory = self

        class Executor:
            def execute(self, current_attempt):
                factory.attempt_ids.append(current_attempt.attempt_id)
                cycle = int(
                    current_attempt.attempt_id.split("/review-fix/c")[1].split("/")[0]
                )
                if stage == "review":
                    findings = (
                        (factory._FINDING,) if cycle in {1, 2} else ()
                    )
                    return TaskResult(
                        current_attempt.attempt_id,
                        "succeeded",
                        semantic_payload(
                            summary="Review complete.",
                            details_schema="review-fix-review/1",
                            details={},
                            findings=findings,
                        ),
                    )
                schema = f"review-fix-{stage}/1"
                key = (
                    "addressed_finding_keys"
                    if stage == "fix"
                    else "verified_finding_keys"
                )
                return TaskResult(
                    current_attempt.attempt_id,
                    "succeeded",
                    semantic_payload(
                        summary=f"{stage} complete.",
                        details_schema=schema,
                        details={key: [factory.KEY]},
                        findings=(),
                    ),
                )

        return Executor()


_PLAN_SHA = hashlib.sha256(b"plan\n").hexdigest()


class _InterruptedRepairExecutor:
    def execute(self, attempt):
        raise InterruptedError("repair worker connection dropped")


class _FlakyRepairFactory:
    """Repair worker whose stream dies a fixed number of times, then repairs.

    Each dropped stream is an infrastructure transient: the deterministic half
    of the composed policy retries it, and each retry spends general recovery
    budget without saying anything about the candidate under review.
    """

    def __init__(self, worktree: Path, failures: int) -> None:
        self.worktree = worktree
        self.failures = failures
        self.calls = 0

    def __call__(self, worktree, attempt):
        factory = self

        class Executor:
            def execute(self, current_attempt):
                factory.calls += 1
                if factory.calls <= factory.failures:
                    raise InterruptedError("repair worker connection dropped")
                (factory.worktree / "verified.txt").write_text(
                    "repaired\n", encoding="utf-8"
                )
                return TaskResult(
                    current_attempt.attempt_id,
                    "succeeded",
                    {"summary": "Applied bounded verification repair."},
                )

        return Executor()


class FeatureRunTests(unittest.TestCase):
    def test_verification_failure_classifier_is_conservative_and_rule_bound(self) -> None:
        transient = classify_verification_failure(
            {"stderr": "temporary failure resolving DNS", "stdout": ""}
        )
        self.assertEqual(transient["classification"], "infrastructure_transient")
        self.assertEqual(transient["rule_id"], "transient-network")
        timeout = classify_verification_failure(
            {"stderr": "browser selector timed out", "stdout": ""}
        )
        self.assertEqual(timeout["classification"], "indeterminate")
        self.assertEqual(timeout["rule_id"], "conservative-default")
        selector_failure = classify_verification_failure(
            {"stderr": "browser selector failed", "stdout": ""}
        )
        self.assertEqual(selector_failure["classification"], "indeterminate")
        self.assertEqual(selector_failure["rule_id"], "conservative-default")

    @patch("harness_labs.featurerun.feature_run.run_feature_worktree")
    def test_plan_graph_mode_omits_only_orientation_and_planning(
        self, run_feature
    ) -> None:
        criteria = (
            {
                "id": "AC-1",
                "statement": "The approved feature is implemented.",
                "source": "plan",
            },
        )
        plan_bytes = b"registered plan\n"
        plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
        binding = PlanGraphFeatureRunBinding(
            plan_graph_id="graph-1",
            plan_node_id="FR-01",
            objective="Implement the approved feature.",
            acceptance_criteria=criteria,
            approved_plan={"path": "docs/plan.md", "sha256": plan_sha256},
            source_binding_report={"claims": ["approved"]},
            build_briefing={"allowed_paths": ["feature.txt"]},
            plan="docs/plan.md",
            plan_base_commit="a" * 40,
            plan_sha256=plan_sha256,
            allowed_paths=("feature.txt",),
            verification_argv=("python3", "-m", "unittest"),
            verification_timeout_seconds=1200,
        )
        normal_schema = standard_feature_run_dispatch_schema()
        normal_phases = tuple(
            phase for segment in normal_schema.segments for phase in segment.phases
        )

        def contract_factory(worktree, receipt):
            return RunContract(
                run_id="graph-1-FR-01",
                objective=binding.objective,
                phases=normal_phases,
                criteria=criteria,
                repository={"path": str(worktree), **receipt},
            )

        sentinel = object()
        run_feature.return_value = sentinel
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=plan_bytes, stderr=b""
            )
            result = run_plan_graph_feature_worktree(
                binding=binding,
                schema=normal_schema,
                contract_factory=contract_factory,
                review_fix_policy=ReviewFixPolicy(),
                base_repository=Path("repository"),
                verification_repair_executor_factory=lambda attempt: None,
            )
        self.assertEqual(
            git_show.call_args.args[0],
            ["git", "show", f"{'a' * 40}:docs/plan.md"],
        )

        self.assertIs(result, sentinel)
        options = run_feature.call_args.kwargs
        bound_phases = tuple(
            phase for segment in options["schema"].segments for phase in segment.phases
        )
        self.assertEqual(
            bound_phases,
            ("implement",),
        )
        self.assertEqual(options["review_finding_obligations"], ())
        self.assertIsNone(options["review_finding_transfer_targets"])
        self.assertEqual(options["review_origin_node_id"], "FR-01")
        self.assertFalse(options["review_inherited_ledger_frozen"])
        # Every PlanGraph-bound run gets the default recovery agent; without
        # one each `_recover_abnormal` call site is inert and an exhausted
        # review loop blocks the node instead of continuing its ledger.
        #
        # It must be the *composed* agent. Binding the continuation policy
        # alone here would silently drop transient retry -- which is
        # run_feature_worktree's own default -- for every campaign that does
        # not pass an agent explicitly, so an infrastructure blip would block
        # the node instead of costing a bounded retry. Asserted by behaviour
        # rather than identity, because the composition is what matters.
        bound_agent = options["recovery_agent"]
        self.assertIsNotNone(bound_agent)
        self.assertEqual(
            bound_agent(
                _recovery_context(
                    stage="implement",
                    condition="failed",
                    reason="terminal_reason aborted_streaming",
                )
            ).action,
            "retry",
            "the PlanGraph default must keep P1's transient retry",
        )
        self.assertEqual(
            bound_agent(_recovery_context()).action,
            "retry",
            "the PlanGraph default must keep the review continuation",
        )
        bound_instructions = options["schema"].segments[0].instructions
        self.assertIn(
            "Dispatch only implementation or implementation-repair tasks",
            bound_instructions,
        )
        self.assertIn("parent FeatureRun owns", bound_instructions)
        self.assertNotIn("leave a tested candidate", bound_instructions)
        self.assertEqual(
            [artifact.kind for artifact in options["initial_evidence"]],
            ["engineering-plan", "source-binding-report", "build-briefing"],
        )
        self.assertEqual(options["allowed_paths"], ("feature.txt",))
        self.assertEqual(
            options["verification_argv"], ("python3", "-m", "unittest")
        )
        self.assertEqual(options["verification_timeout_seconds"], 1200)
        handoff = options["initial_evidence"][0].content
        self.assertEqual(handoff["plan_graph_id"], "graph-1")
        self.assertEqual(handoff["plan_node_id"], "FR-01")
        bound_contract = options["contract_factory"](
            Path("worktree"),
            {"feature_branch": "feature", "base_branch": "main", "base_commit": "b" * 40},
        )
        self.assertEqual(bound_contract.phases, bound_phases)
        self.assertEqual(bound_contract.criteria, criteria)

        # Exercise the real kernel criterion path (not a mock) with the
        # plan-graph binding's source, since a kernel that rejects it would
        # crash every plan-graph-bound feature run at construction time.
        bound_kernel = ControllerKernel(bound_contract, evidence=EvidenceCatalog())
        self.assertEqual(
            bound_kernel.snapshot()["criteria"]["AC-1"]["source"],
            "plan",
        )

        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=plan_bytes, stderr=b""
            )
            with self.assertRaisesRegex(
                ValueError, "override controller-owned values"
            ):
                run_plan_graph_feature_worktree(
                    binding=binding,
                    schema=normal_schema,
                    contract_factory=contract_factory,
                    review_fix_policy=ReviewFixPolicy(),
                    base_repository=Path("repository"),
                    allowed_paths=("everything",),
                    verification_repair_executor_factory=lambda attempt: None,
                )

    @patch("harness_labs.featurerun.feature_run.run_feature_worktree")
    def test_plan_graph_mode_binds_transfer_obligations_to_review(self, run_feature) -> None:
        plan_bytes = b"registered plan\n"
        binding = PlanGraphFeatureRunBinding(
            plan_graph_id="graph-1", plan_node_id="FR-01", objective="Implement.",
            acceptance_criteria=({"id": "AC-1", "statement": "Works"},),
            approved_plan={"path": "docs/plan.md", "sha256": hashlib.sha256(plan_bytes).hexdigest()},
            source_binding_report={"claims": ["approved"]},
            build_briefing={"allowed_paths": ["feature.txt"]},
            plan="docs/plan.md", plan_base_commit="a" * 40,
            plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            allowed_paths=("feature.txt",), verification_argv=("python3", "-m", "unittest"),
            finding_obligations=({"key": "feature.txt:transfer"},),
            finding_transfer_targets={"feature.txt": "FR-02"},
            origin_node_id="FR-01", inherited_ledger_frozen=True,
            bounded_fix_only=True,
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess([], 0, stdout=plan_bytes, stderr=b"")
            run_plan_graph_feature_worktree(
                binding=binding, schema=standard_feature_run_dispatch_schema(),
                contract_factory=lambda worktree, receipt: None,
                review_fix_policy=ReviewFixPolicy(), base_repository=Path("repository"),
                verification_repair_executor_factory=lambda attempt: None,
            )
        options = run_feature.call_args.kwargs
        self.assertEqual(options["review_finding_obligations"], binding.finding_obligations)
        self.assertEqual(options["review_finding_transfer_targets"], binding.finding_transfer_targets)
        self.assertEqual(options["review_origin_node_id"], "FR-01")
        self.assertTrue(options["review_inherited_ledger_frozen"])
        self.assertTrue(options["review_bounded_fix_only"])

    def test_plan_graph_binding_bounded_fix_only_defaults_false(self) -> None:
        plan_bytes = b"registered plan\n"
        binding = PlanGraphFeatureRunBinding(
            plan_graph_id="graph-1", plan_node_id="FR-01", objective="Implement.",
            acceptance_criteria=({"id": "AC-1", "statement": "Works"},),
            approved_plan={"path": "docs/plan.md", "sha256": hashlib.sha256(plan_bytes).hexdigest()},
            source_binding_report={"claims": ["approved"]},
            build_briefing={"allowed_paths": ["feature.txt"]},
            plan="docs/plan.md", plan_base_commit="a" * 40,
            plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            allowed_paths=("feature.txt",), verification_argv=("python3", "-m", "unittest"),
        )
        self.assertFalse(binding.bounded_fix_only)
        with self.assertRaisesRegex(ValueError, "bounded_fix_only requires"):
            PlanGraphFeatureRunBinding(
                plan_graph_id="graph-1", plan_node_id="FR-01", objective="Implement.",
                acceptance_criteria=({"id": "AC-1", "statement": "Works"},),
                approved_plan={"path": "docs/plan.md", "sha256": hashlib.sha256(plan_bytes).hexdigest()},
                source_binding_report={"claims": ["approved"]},
                build_briefing={"allowed_paths": ["feature.txt"]},
                plan="docs/plan.md", plan_base_commit="a" * 40,
                plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
                allowed_paths=("feature.txt",), verification_argv=("python3", "-m", "unittest"),
                bounded_fix_only=True,
            )

    def test_plan_graph_mode_refuses_disabled_review_ledger(self) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        plan_sha256 = hashlib.sha256(b"plan\n").hexdigest()
        binding = PlanGraphFeatureRunBinding(
            "graph-1",
            "FR-01",
            "Build it",
            criteria,
            {"path": "plan.md", "sha256": plan_sha256},
            {"claims": ["bound"]},
            {"allowed_paths": ["feature.txt"]},
            "plan.md",
            "a" * 40,
            plan_sha256,
            ("feature.txt",),
            ("python3", "-m", "unittest"),
            1200,
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"plan\n", stderr=b""
            )
            with self.assertRaisesRegex(ValueError, "ledger-backed review guards"):
                run_plan_graph_feature_worktree(
                    binding=binding,
                    schema=standard_feature_run_dispatch_schema(),
                    contract_factory=lambda worktree, receipt: None,
                    review_fix_policy=ReviewFixPolicy(ledger_enabled=False),
                    base_repository=Path("repository"),
                    verification_repair_executor_factory=lambda attempt: None,
                )

    @patch("harness_labs.featurerun.feature_run.run_feature_worktree")
    def test_plan_graph_mode_rejects_registered_plan_hash_drift(self, run_feature) -> None:
        plan_sha256 = hashlib.sha256(b"approved\n").hexdigest()
        binding = PlanGraphFeatureRunBinding(
            "graph-1",
            "node-1",
            "Build it",
            ({"id": "AC-1", "statement": "Works", "source": "plan"},),
            {"path": "docs/plan.md", "sha256": plan_sha256},
            {"claims": ["bound"]},
            {"allowed_paths": ["feature.txt"]},
            "docs/plan.md",
            "a" * 40,
            plan_sha256,
            ("feature.txt",),
            ("python3", "-m", "unittest"),
            1200,
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"drifted\n", stderr=b""
            )
            with self.assertRaisesRegex(ValueError, "plan hash mismatch"):
                run_plan_graph_feature_worktree(
                    binding=binding,
                    schema=standard_feature_run_dispatch_schema(),
                    contract_factory=lambda worktree, receipt: None,
                    review_fix_policy=ReviewFixPolicy(),
                    base_repository=Path("repository"),
                    verification_repair_executor_factory=lambda attempt: None,
                )
        run_feature.assert_not_called()

    @patch("harness_labs.featurerun.feature_run.run_feature_worktree")
    def test_plan_graph_child_seals_a_lane_without_integration(self, run_feature) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        binding = PlanGraphFeatureRunBinding(
            "graph-1",
            "FR-10",
            "Build a lane",
            criteria,
            {"path": "plan.md", "sha256": _PLAN_SHA},
            {"claims": ["bound"]},
            {"allowed_paths": ["feature.txt"]},
            "plan.md",
            "a" * 40,
            _PLAN_SHA,
            ("feature.txt",),
            ("python3", "-m", "unittest"),
            1200,
            "a" * 40,
            "lane/FR-10",
            Path("lane"),
            1,
            "alloc-fr-10",
            1,
            "a" * 40,
            "batch-1",
            (),
            ("feature.txt",),
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"plan\n", stderr=b""
            )
            run_plan_graph_feature_worktree(
                binding=binding,
                schema=standard_feature_run_dispatch_schema(),
                contract_factory=lambda worktree, receipt: None,
                review_fix_policy=ReviewFixPolicy(),
                base_repository=Path("repository"),
                base_branch="main",
                feature_branch="lane/FR-10",
                worktree_path=Path("lane"),
                run_dir=Path("run"),
                commit_message="Build lane",
                verification_repair_executor_factory=lambda attempt: None,
            )

        options = run_feature.call_args.kwargs
        self.assertEqual(options["base_commit"], "a" * 40)
        self.assertTrue(options["candidate_only"])
        self.assertFalse(options["merge"])

    def test_plan_graph_child_rejects_shared_integration(self) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        binding = PlanGraphFeatureRunBinding(
            "graph-1", "FR-10", "Build a lane", criteria, {"path": "plan.md", "sha256": _PLAN_SHA},
            {"claims": ["bound"]}, {"allowed_paths": ["feature.txt"]},
            "plan.md", "a" * 40, _PLAN_SHA,
            ("feature.txt",), ("python3", "-m", "unittest"), 1200,
            "a" * 40, "lane/FR-10", Path("lane"), 1, "alloc-fr-10", 1, "a" * 40,
            "batch-1", (), ("feature.txt",),
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"plan\n", stderr=b""
            )
            with self.assertRaisesRegex(ValueError, "cannot merge shared integration state"):
                run_plan_graph_feature_worktree(
                    binding=binding,
                    schema=standard_feature_run_dispatch_schema(),
                    contract_factory=lambda worktree, receipt: None,
                    review_fix_policy=ReviewFixPolicy(),
                    base_repository=Path("repository"), base_branch="main",
                    feature_branch="lane/FR-10", worktree_path=Path("lane"),
                    run_dir=Path("run"),
                    commit_message="Build lane", merge=True,
                    verification_repair_executor_factory=lambda attempt: None,
                )

    @patch("harness_labs.featurerun.feature_run.run_feature_worktree")
    def test_plan_graph_child_returns_allocation_bound_canonical_seal_receipt(
        self, run_feature
    ) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        binding = PlanGraphFeatureRunBinding(
            "graph-1", "FR-10", "Build a lane", criteria, {"path": "plan.md", "sha256": _PLAN_SHA},
            {"claims": ["bound"]}, {"allowed_paths": ["feature.txt"]},
            "plan.md", "a" * 40, _PLAN_SHA,
            ("feature.txt",), ("python3", "-m", "unittest"), 1200,
            "a" * 40, "lane/FR-10", Path("lane"), 3, "alloc-fr-10", 7, "a" * 40,
            "batch-3", (), ("feature.txt",),
        )
        verification_command = {"argv": ["python3", "-m", "unittest"], "exit_code": 0}
        verification_bytes = (
            json.dumps(verification_command, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        run_feature.return_value = FeatureRunResult(
            "succeeded", None, None, {},
            ({
                "operation": "commit",
                "base_commit": "a" * 40,
                "candidate_commit": "b" * 40,
                "allowed_paths": ["feature.txt"],
            },),
            {"manifest_hash": "c" * 64, "head_hash": "d" * 64},
            Path("run"), Path("lane"),
            verification=DeterministicVerificationResult(
                "succeeded", "passed",
                ((
                    verification_command
                    | {"evidence_ref": f"artifact:sha256:{hashlib.sha256(verification_bytes).hexdigest()}"}
                ),), 0,
            ),
        )

        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"plan\n", stderr=b""
            )
            result = run_plan_graph_feature_worktree(
                binding=binding,
                schema=standard_feature_run_dispatch_schema(),
                contract_factory=lambda worktree, receipt: None,
                review_fix_policy=ReviewFixPolicy(),
                base_repository=Path("repository"), base_branch="main",
                feature_branch="lane/FR-10", worktree_path=Path("lane"),
                run_dir=Path("run"),
                commit_message="Build lane",
                verification_repair_executor_factory=lambda attempt: None,
            )

        self.assertEqual(
            [artifact.kind for artifact in run_feature.call_args.kwargs["initial_evidence"]],
            ["engineering-plan", "source-binding-report", "build-briefing", "plan-graph-child-request"],
        )
        self.assertEqual(result.seal_receipt["protocol"], "harness-plan-graph-parallel-seal-receipt/1")
        self.assertEqual(result.seal_receipt["allocation_id"], "alloc-fr-10")
        self.assertEqual(result.seal_receipt["candidate_commit"], "b" * 40)
        self.assertEqual(result.seal_receipt["canonical_manifest_ref"], f"artifact:sha256:{'c' * 64}")
        descriptor = run_feature.call_args.kwargs["initial_evidence"][-1].content
        self.assertEqual(descriptor["protocol"], "harness-plan-graph-parallel-child-request/1")
        self.assertEqual(descriptor["allocation"]["batch_id"], "batch-3")
        self.assertEqual(descriptor["lane"]["may_advance_staging"], False)
        self.assertEqual(descriptor["writable_paths"], ["feature.txt"])
        descriptor_bytes = (
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.assertEqual(
            result.seal_receipt["descriptor_ref"],
            f"artifact:sha256:{hashlib.sha256(descriptor_bytes).hexdigest()}",
        )

    @patch("harness_labs.featurerun.feature_run.run_feature_worktree")
    def test_plan_graph_child_preserves_the_complete_dependency_order(self, run_feature) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        dependencies = (
            {"node_id": "FR-10", "candidate_commit": "1" * 40, "seal_receipt_ref": f"artifact:sha256:{'1' * 64}"},
            {"node_id": "FR-11", "candidate_commit": "2" * 40, "seal_receipt_ref": f"artifact:sha256:{'2' * 64}"},
            {"node_id": "FR-12", "candidate_commit": "3" * 40, "seal_receipt_ref": f"artifact:sha256:{'3' * 64}"},
        )
        binding = PlanGraphFeatureRunBinding(
            "graph-1", "FR-20", "Join lanes", criteria, {"path": "plan.md", "sha256": _PLAN_SHA},
            {"claims": ["bound"]}, {"allowed_paths": ["integration"]},
            "plan.md", "a" * 40, _PLAN_SHA,
            ("integration",), ("python3", "-m", "unittest"), 1200,
            "a" * 40, "lane/FR-20", Path("lane"), 8, "alloc-fr-20", 8, "a" * 40,
            "batch-2", dependencies, ("integration",),
        )
        descriptor = binding.child_descriptor()

        self.assertEqual(descriptor["dependency_candidates"], list(dependencies))
        self.assertEqual(descriptor["allocation"], {
            "batch_id": "batch-2", "logical_attempt": 8,
            "allocation_id": "alloc-fr-20", "checkpoint_revision": 8,
            "expected_staging_head": "a" * 40,
        })
        self.assertEqual(binding.handoff_artifacts()[-1].content, descriptor)

    def test_plan_graph_child_rejects_writable_path_substitution(self) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        binding = PlanGraphFeatureRunBinding(
            "graph-1", "FR-10", "Build a lane", criteria, {"path": "plan.md", "sha256": _PLAN_SHA},
            {"claims": ["bound"]}, {"allowed_paths": ["substituted.txt"]},
            "plan.md", "a" * 40, _PLAN_SHA,
            ("substituted.txt",), ("python3", "-m", "unittest"), 1200,
            "a" * 40, "lane/FR-10", Path("lane"), 1, "alloc-fr-10", 1, "a" * 40,
            "batch-1", (), ("feature.txt",),
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"plan\n", stderr=b""
            )
            with self.assertRaisesRegex(ValueError, "allowed_paths must match"):
                run_plan_graph_feature_worktree(
                    binding=binding, schema=standard_feature_run_dispatch_schema(),
                    contract_factory=lambda worktree, receipt: None,
                    review_fix_policy=ReviewFixPolicy(), base_repository=Path("repository"),
                    base_branch="main", feature_branch="lane/FR-10", worktree_path=Path("lane"),
                    run_dir=Path("run"),
                    commit_message="Build lane",
                    verification_repair_executor_factory=lambda attempt: None,
                )

    def test_plan_graph_child_rejects_schema_invalid_fields(self) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        child_fields = {
            "parent_candidate_commit": "a" * 40,
            "lane_branch": "lane/FR-10",
            "lane_worktree": Path("lane"),
            "logical_attempt": 1,
            "allocation_id": "alloc-fr-10",
            "checkpoint_revision": 1,
            "expected_staging_head": "a" * 40,
            "batch_id": "batch-1",
            "dependency_candidates": (),
            "writable_paths": ("feature.txt",),
        }
        cases = (
            ("Unicode graph ID", {"plan_graph_id": "gr\u00e1ph-1"}, "identifiers"),
            ("Unicode dependency ID", {"dependency_candidates": (
                {"node_id": "FR-\u00e9", "candidate_commit": "b" * 40,
                 "seal_receipt_ref": f"artifact:sha256:{'b' * 64}"},
            )}, "node_id"),
            ("boolean logical attempt", {"logical_attempt": True}, "logical_attempt"),
            ("boolean checkpoint revision", {"checkpoint_revision": True},
             "checkpoint_revision"),
            ("empty writable paths", {"writable_paths": ()},
             "writable_paths must not be empty"),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                PlanGraphFeatureRunBinding(**({
                    "plan_graph_id": "graph-1",
                    "plan_node_id": "FR-10",
                    "objective": "Build a lane",
                    "acceptance_criteria": criteria,
                    "approved_plan": {"path": "plan.md", "sha256": _PLAN_SHA},
                    "source_binding_report": {"claims": ["bound"]},
                    "build_briefing": {"allowed_paths": ["feature.txt"]},
                    "plan": "plan.md",
                    "plan_base_commit": "a" * 40,
                    "plan_sha256": _PLAN_SHA,
                    "allowed_paths": ("feature.txt",),
                    "verification_argv": ("python3", "-m", "unittest"),
                    "verification_timeout_seconds": 1200,
                } | child_fields | overrides))

    def test_plan_graph_child_rejects_an_unallocated_worktree(self) -> None:
        criteria = ({"id": "AC-1", "statement": "Works", "source": "plan"},)
        binding = PlanGraphFeatureRunBinding(
            "graph-1", "FR-10", "Build a lane", criteria, {"path": "plan.md", "sha256": _PLAN_SHA},
            {"claims": ["bound"]}, {"allowed_paths": ["feature.txt"]},
            "plan.md", "a" * 40, _PLAN_SHA,
            ("feature.txt",), ("python3", "-m", "unittest"), 1200,
            "a" * 40, "lane/FR-10", Path("allocated-lane"), 1, "alloc-fr-10", 1, "a" * 40,
            "batch-1", (), ("feature.txt",),
        )
        with patch("harness_labs.featurerun.feature_run.subprocess.run") as git_show:
            git_show.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"plan\n", stderr=b""
            )
            with self.assertRaisesRegex(ValueError, "worktree_path must match"):
                run_plan_graph_feature_worktree(
                    binding=binding,
                    schema=standard_feature_run_dispatch_schema(),
                    contract_factory=lambda worktree, receipt: None,
                    review_fix_policy=ReviewFixPolicy(),
                    base_repository=Path("repository"), base_branch="main",
                    feature_branch="lane/FR-10", worktree_path=Path("substituted-lane"),
                    run_dir=Path("run"),
                    commit_message="Build lane",
                    verification_repair_executor_factory=lambda attempt: None,
                )

    def test_plan_graph_feature_run_has_no_skill_or_serial_coupling(self) -> None:
        import harness_labs.featurerun.feature_run as feature_run_module
        import harness_labs.featurerun.feature_run_policy as feature_run_policy_module

        material = "\n".join(
            (
                inspect.getsource(feature_run_module),
                inspect.getsource(feature_run_policy_module),
                json.dumps(
                    standard_feature_run_dispatch_schema().as_dict(),
                    sort_keys=True,
                ),
            )
        )
        forbidden = (
            "-".join(("implement", "v13")),
            "_".join(("implement", "v13")),
            "-".join(("serial", "implement")),
            "_".join(("serial", "implement")),
        )
        for value in forbidden:
            self.assertNotIn(value, material)

    def test_deterministic_verification_rejects_model_verify_phase(self) -> None:
        schema = CoordinatorDispatchSchema(
            "invalid-double-verification/1",
            (
                CoordinatorSegment(
                    id="verify",
                    phases=("verify",),
                    instructions="Ask a model to verify.",
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "coordinator verify phase"):
            run_feature_worktree(
                base_repository=Path("unused"),
                base_branch="main",
                feature_branch="feature/test",
                worktree_path=Path("unused-worktree"),
                run_dir=Path("unused-run"),
                contract_factory=lambda worktree, receipt: None,
                schema=schema,
                session_factory=lambda worktree, launch, evidence: None,
                profile_builder=lambda worktree, evidence: (),
                allowed_paths=("feature.txt",),
                commit_message="unused",
                verification_argv=("python3", "-m", "unittest"),
                verification_repair_executor_factory=lambda attempt: None,
            )

    def test_feature_run_creates_commits_and_leaves_merge_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            base.mkdir()
            git(base, "init", "-b", "main")
            git(base, "config", "user.name", "Harness Tests")
            git(base, "config", "user.email", "harness@example.invalid")
            (base / "README.md").write_text("base\n", encoding="utf-8")
            git(base, "add", "README.md")
            git(base, "commit", "--no-gpg-sign", "-m", "Base")
            base_head = git(base, "rev-parse", "HEAD")
            schema = CoordinatorDispatchSchema(
                "feature-test/1",
                (
                    CoordinatorSegment(
                        id="active",
                        phases=("active",),
                        instructions="Build and complete.",
                        required_artifact_kinds=("engineering-plan",),
                        context_artifact_kinds=("engineering-plan",),
                    ),
                ),
            )
            launches = []

            def contract_factory(worktree, receipt):
                return RunContract(
                    run_id="feature-run",
                    objective="Build a file.",
                    phases=("active",),
                    criteria=(
                        {
                            "id": "built",
                            "statement": "The file is built.",
                            "source": "operator",
                        },
                    ),
                    terminal_artifact_kinds=("implementation-summary",),
                    repository={
                        "path": str(worktree),
                        "branch": receipt["feature_branch"],
                        "base_branch": receipt["base_branch"],
                        "base_commit": receipt["base_commit"],
                    },
                )

            def session_factory(worktree, launch, evidence):
                launches.append(launch)
                return ScriptedCoordinatorSession(
                    [
                        (
                            "task_dispatch",
                            {
                                "tasks": [
                                    {
                                        "id": "build",
                                        "role": "builder",
                                        "objective": "Build feature.txt",
                                        "details_schema": "build/1",
                                        "required_capabilities": ["repo.write"],
                                        "acceptance_criteria": ["built"],
                                        "dependencies": [],
                                    }
                                ],
                                "max_parallelism": 1,
                            },
                        ),
                        ("run_complete_request", {}),
                    ],
                    final="Complete.",
                )

            result = run_feature_worktree(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/test",
                worktree_path=root / "feature",
                run_dir=root / "run",
                contract_factory=contract_factory,
                schema=schema,
                session_factory=session_factory,
                profile_builder=lambda worktree, evidence: (
                    RoleProfile(
                        "builder",
                        "builder",
                        frozenset({"repo.write"}),
                        lambda task: _BuildExecutor(task, worktree, evidence),
                    ),
                ),
                allowed_paths=("feature.txt", "reviewed.txt"),
                commit_message="Build feature",
                merge=False,
                review_fix_executor_factory=_ReviewRepairFactory(root / "feature"),
                review_fix_policy=ReviewFixPolicy(),
                verification_argv=(
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('feature.txt').read_text() == 'built\\n'",
                ),
                verification_repair_executor_factory=lambda attempt: self.fail(
                    "repair must not run when deterministic verification passes"
                ),
                evidence_classification="component",
                initial_evidence=(
                    FeatureRunHandoffArtifact(
                        "engineering-plan",
                        {"objective": "Build a file."},
                    ),
                ),
            )

            self.assertEqual(
                result.status,
                "succeeded",
                (
                    result.review_fix.reason
                    if result.review_fix
                    else result.dispatch.result.payload
                ),
            )
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(
                [
                    item["kind"]
                    for item in launches[0].context["handoff_artifacts"]
                ],
                ["engineering-plan"],
            )
            self.assertEqual(result.verification.repair_attempts, 0)
            self.assertEqual(
                [attempt["stage"] for attempt in result.verification.command_attempts],
                ["post_implementation", "post_review_repair"],
            )
            self.assertIsNotNone(result.review_fix)
            self.assertEqual(result.review_fix.cycles, 2)
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create", "commit", "integrate"],
            )
            self.assertEqual(result.git_receipts[-1]["status"], "ready_not_merged")
            self.assertEqual(git(base, "rev-parse", "HEAD"), base_head)
            self.assertNotEqual(
                git(result.worktree_path, "rev-parse", "HEAD"),
                base_head,
            )
            AuditJournal.verify(root / "run")

    # AC-CB06-2/AC-CB06-1: on the build-only segment used by the plan-graph
    # bound dispatch path, the coordinator completes with a gate-backed
    # criterion still pending, and the criterion is satisfied only once the
    # controller-owned deterministic verification command actually passes.
    def test_gate_backed_criterion_is_satisfied_by_verification_not_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            base.mkdir()
            git(base, "init", "-b", "main")
            git(base, "config", "user.name", "Harness Tests")
            git(base, "config", "user.email", "harness@example.invalid")
            (base / "README.md").write_text("base\n", encoding="utf-8")
            git(base, "add", "README.md")
            git(base, "commit", "--no-gpg-sign", "-m", "Base")
            schema = CoordinatorDispatchSchema(
                "feature-gate-test/1",
                (
                    CoordinatorSegment(
                        id="active",
                        phases=("active",),
                        instructions=(
                            "Implement the change. Do not dispatch a "
                            "verification-only task; the parent FeatureRun "
                            "owns and runs the declared verification gate."
                        ),
                        required_artifact_kinds=("engineering-plan",),
                        context_artifact_kinds=("engineering-plan",),
                    ),
                ),
            )

            def contract_factory(worktree, receipt):
                return RunContract(
                    run_id="feature-run-gate",
                    objective="Build a file behind a deterministic gate.",
                    phases=("active",),
                    criteria=(
                        {
                            "id": "built",
                            "statement": "The file is built.",
                            "source": "operator",
                        },
                        {
                            "id": "verified",
                            "statement": (
                                "The declared verification command passes."
                            ),
                            "source": "operator",
                            "adjudication": "deterministic_verification",
                        },
                    ),
                    terminal_artifact_kinds=("implementation-summary",),
                    repository={
                        "path": str(worktree),
                        "branch": receipt["feature_branch"],
                        "base_branch": receipt["base_branch"],
                        "base_commit": receipt["base_commit"],
                    },
                )

            def session_factory(worktree, launch, evidence):
                # The coordinator can only truthfully claim "built"; nothing
                # in its tool surface can claim "verified" (AC-CB06-3 makes a
                # direct claim on it a kernel rejection), so it completes
                # with "verified" still pending.
                return ScriptedCoordinatorSession(
                    [
                        (
                            "task_dispatch",
                            {
                                "tasks": [
                                    {
                                        "id": "build",
                                        "role": "builder",
                                        "objective": "Build feature.txt",
                                        "details_schema": "build/1",
                                        "required_capabilities": ["repo.write"],
                                        "acceptance_criteria": ["built"],
                                        "dependencies": [],
                                    }
                                ],
                                "max_parallelism": 1,
                            },
                        ),
                        ("run_complete_request", {}),
                    ],
                    final="Complete.",
                )

            result = run_feature_worktree(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/gate-test",
                worktree_path=root / "feature",
                run_dir=root / "run",
                contract_factory=contract_factory,
                schema=schema,
                session_factory=session_factory,
                profile_builder=lambda worktree, evidence: (
                    RoleProfile(
                        "builder",
                        "builder",
                        frozenset({"repo.write"}),
                        lambda task: _BuildExecutor(task, worktree, evidence),
                    ),
                ),
                allowed_paths=("feature.txt",),
                commit_message="Build feature behind a gate",
                merge=False,
                verification_argv=(
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('feature.txt').read_text() == 'built\\n'",
                ),
                verification_repair_executor_factory=lambda attempt: self.fail(
                    "repair must not run when deterministic verification passes"
                ),
                evidence_classification="component",
                initial_evidence=(
                    FeatureRunHandoffArtifact(
                        "engineering-plan",
                        {"objective": "Build a file behind a deterministic gate."},
                    ),
                ),
            )

            self.assertEqual(
                result.status,
                "succeeded",
                result.dispatch.result.payload,
            )
            self.assertEqual(result.verification.status, "succeeded")
            criteria = {item["id"]: item for item in result.run_view["criteria"]}
            self.assertEqual(criteria["built"]["status"], "satisfied")
            self.assertEqual(criteria["verified"]["status"], "satisfied")
            self.assertIn("verification-owner", criteria["verified"]["satisfied_by"])
            self.assertNotIn("build", criteria["verified"]["satisfied_by"])
            AuditJournal.verify(root / "run")

    def test_failed_verification_repairs_same_candidate_and_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts = []
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    seen_contexts,
                ),
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [7, 0],
            )
            self.assertEqual(
                seen_contexts[0]["failed_verification"]["stdout"],
                "verification failed\n",
            )
            self.assertEqual(
                seen_contexts[0]["failed_verification"]["stderr"],
                "missing verified.txt\n",
            )
            self.assertEqual(
                (result.worktree_path / "verified.txt").read_text(encoding="utf-8"),
                "repaired\n",
            )
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create", "commit", "integrate"],
            )
            AuditJournal.verify(root / "run")

    def test_exhausted_repair_blocks_without_discarding_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts = []
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    seen_contexts,
                    repair=False,
                ),
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.verification.status, "blocked")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [7, 7],
            )
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create"],
            )
            self.assertEqual(
                (result.worktree_path / "feature.txt").read_text(encoding="utf-8"),
                "built\n",
            )
            self.assertTrue(result.worktree_path.exists())
            AuditJournal.verify(root / "run")

    def test_blocked_verification_uses_bounded_recovery_and_records_plan_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts = []
            recovery_agent = _VerificationRecoveryAgent(root / "feature")
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    seen_contexts,
                    repair=False,
                ),
                recovery_agent=recovery_agent,
                recovery_limit=1,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [7, 7, 0],
            )
            self.assertEqual(len(recovery_agent.contexts), 1)
            context = recovery_agent.contexts[0]
            self.assertEqual(context.stage, "verification")
            self.assertEqual(context.condition, "blocked")
            self.assertEqual(context.checkpoint["status"], "recovering")
            self.assertEqual(
                context.acceptance_criteria[0]["id"],
                "built",
            )
            checkpoint = json.loads(
                (root / "run" / "checkpoint.json").read_text(encoding="utf-8")
            )
            decision = checkpoint["state"]["recovery"]["decisions"][0]
            self.assertEqual(decision["action"], "adjust_plan")
            self.assertEqual(
                decision["plan_adjustment"]["add_step"],
                "Create and verify the recovery marker.",
            )
            events = [
                json.loads(line)
                for line in (root / "run" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            recovery_events = [
                event
                for event in events
                if event["event_type"] == "recovery_decision"
            ]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(recovery_events[0]["status"], "succeeded")
            AuditJournal.verify(root / "run")

    def test_blocked_review_continues_its_ledger_without_reimplementing(self) -> None:
        """The whole point of steps 1-2: an exhausted review continues.

        The implementation and the ledger it was reviewed against are both
        already paid for, so the continuation spends its grant on the open
        finding rather than re-running implementation and rediscovering it.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review_factory = _ContinuationReviewFactory()
            contexts = []

            def recovery_agent(context):
                contexts.append(context)
                return standard_review_continuation_recovery_agent()(context)

            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(worktree, []),
                recovery_agent=recovery_agent,
                recovery_limit=2,
                review_fix_executor_factory=review_factory,
                review_fix_policy=ReviewFixPolicy(
                    mechanical_cycle_limit=1, continuation_cycles=2
                ),
            )

            self.assertEqual(result.status, "succeeded")
            # One continuation was granted, on the review stage, after the
            # cycle limit blocked a loop that had found real work.
            self.assertEqual(len(contexts), 1)
            self.assertEqual(contexts[0].stage, "review")
            self.assertEqual(contexts[0].condition, "blocked")
            self.assertEqual(
                contexts[0].stage_detail["open_finding_keys"],
                [_ContinuationReviewFactory.KEY],
            )
            self.assertEqual(contexts[0].stage_detail["cycles_spent"], 1)
            # Cycle numbering continues across the grant -- a restart would
            # have replayed c1 and collided on its attempt ids.
            self.assertEqual(
                review_factory.attempt_ids,
                [
                    "feature-verification-run/review-fix/c1/review",
                    "feature-verification-run/review-fix/c2/review",
                    "feature-verification-run/review-fix/c2/fix",
                    "feature-verification-run/review-fix/c2/verify",
                    "feature-verification-run/review-fix/c3/review",
                ],
            )
            self.assertEqual(result.review_fix.cycles, 3)
            self.assertEqual(result.review_fix.open_finding_keys, ())
            # The candidate was committed, so the node seals instead of
            # blocking and no successor attempt re-implements it.
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create", "commit", "integrate"],
            )
            AuditJournal.verify(root / "run")

    def _recovery_decisions(self, root):
        checkpoint = json.loads(
            (root / "run" / "checkpoint.json").read_text(encoding="utf-8")
        )
        return checkpoint["state"]["recovery"]["decisions"]

    def test_transient_retries_do_not_starve_a_later_review_continuation(
        self,
    ) -> None:
        """Stream deaths must not spend the review continuation's budget.

        With one shared counter, a run that burned its whole recovery limit on
        infrastructure transients reached its first continuation opportunity
        with nothing left, and the continuation was denied on budget before
        the policy was ever asked. Three dropped repair streams exhaust the
        general budget here; the continuation must still be granted, and the
        run must seal instead of blocking.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review_factory = _ContinuationReviewFactory()
            result = self._run_verification_recovery_case(
                root,
                _FlakyRepairFactory(root / "feature", failures=3),
                recovery_agent=standard_composed_recovery_agent(),
                review_fix_executor_factory=review_factory,
                review_fix_policy=ReviewFixPolicy(
                    mechanical_cycle_limit=1, continuation_cycles=2
                ),
            )

            self.assertEqual(result.status, "succeeded")
            decisions = self._recovery_decisions(root)
            self.assertEqual(
                [item["recovery_class"] for item in decisions],
                ["general", "general", "general", "review_continuation"],
            )
            # The general budget is spent to its limit and the continuation is
            # still granted, on its own allowance.
            self.assertEqual(
                [item["action"] for item in decisions],
                ["retry", "retry", "retry", "retry"],
            )
            self.assertEqual(
                decisions[-1]["budget"],
                {
                    "class": "review_continuation",
                    "class_attempt": 1,
                    "class_limit": 2,
                    "total_attempt": 4,
                    "total_limit": 5,
                },
            )
            self.assertEqual(decisions[2]["budget"]["class_attempt"], 3)
            # The continuation actually continued the ledger: cycle numbering
            # carried on past the exhausted limit and cleared the finding.
            self.assertEqual(result.review_fix.cycles, 3)
            self.assertEqual(result.review_fix.open_finding_keys, ())
            self.assertIn(
                "feature-verification-run/review-fix/c3/review",
                review_factory.attempt_ids,
            )
            AuditJournal.verify(root / "run")

    def test_budget_exhaustion_is_recorded_apart_from_a_policy_stop(self) -> None:
        """Evidence must say which of the two denied the recovery.

        A continuation refused because no budget remained and a continuation
        refused because the policy judged it pointless are different events
        with different fixes, and before this they were distinguishable only by
        reading a free-text reason.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(worktree, []),
                recovery_agent=standard_composed_recovery_agent(),
                continuation_recovery_limit=0,
                review_fix_executor_factory=_ContinuationReviewFactory(),
                review_fix_policy=ReviewFixPolicy(
                    mechanical_cycle_limit=1, continuation_cycles=2
                ),
            )

            self.assertEqual(result.status, "blocked")
            denial = self._recovery_decisions(root)[-1]
            self.assertEqual(denial["action"], "stop")
            self.assertEqual(denial["stop_cause"], "budget_exhausted")
            self.assertEqual(denial["recovery_class"], "review_continuation")
            self.assertEqual(denial["budget"]["class_limit"], 0)
            self.assertIn(
                "review-continuation recovery limit of 0 exhausted",
                denial["reason"],
            )
            AuditJournal.verify(root / "run")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    [],
                    repair=False,
                ),
                recovery_agent=lambda context: RecoveryDecision(
                    "stop", "The verification failure is a real defect."
                ),
            )

            self.assertEqual(result.status, "blocked")
            stopped = self._recovery_decisions(root)[-1]
            self.assertEqual(stopped["action"], "stop")
            self.assertEqual(stopped["stop_cause"], "policy")
            self.assertEqual(stopped["recovery_class"], "general")
            self.assertLess(
                stopped["budget"]["class_attempt"], stopped["budget"]["class_limit"] + 1
            )
            AuditJournal.verify(root / "run")

    def test_explicit_recovery_limit_still_bounds_general_recoveries(self) -> None:
        """An explicit ``recovery_limit`` keeps meaning what it meant.

        The separate continuation allowance must not leak into the general
        class: a caller that asked for one recovery still gets exactly one.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_verification_recovery_case(
                root,
                _FlakyRepairFactory(root / "feature", failures=5),
                recovery_agent=standard_composed_recovery_agent(),
                recovery_limit=1,
            )

            # The unrecovered interruption ends the run; what matters is that
            # it ended after exactly one general recovery.
            self.assertEqual(result.status, "failed")
            decisions = self._recovery_decisions(root)
            self.assertEqual(
                [item["recovery_class"] for item in decisions],
                ["general", "general"],
            )
            self.assertEqual([item["action"] for item in decisions], ["retry", "stop"])
            self.assertIn("recovery limit of 1 exhausted", decisions[-1]["reason"])
            self.assertEqual(decisions[-1]["stop_cause"], "budget_exhausted")
            AuditJournal.verify(root / "run")

    def test_recovery_limit_stops_repeated_abnormal_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery_contexts = []

            def recovery_agent(context):
                recovery_contexts.append(context)
                return RecoveryDecision(
                    "retry",
                    "Retry verification after refreshing transient state.",
                )

            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    [],
                    repair=False,
                ),
                recovery_agent=recovery_agent,
                recovery_limit=1,
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(len(recovery_contexts), 1)
            checkpoint = json.loads(
                (root / "run" / "checkpoint.json").read_text(encoding="utf-8")
            )
            decisions = checkpoint["state"]["recovery"]["decisions"]
            self.assertEqual([item["action"] for item in decisions], ["retry", "stop"])
            self.assertIn("limit of 1 exhausted", decisions[-1]["reason"])
            AuditJournal.verify(root / "run")

    def test_failed_review_is_retried_after_recovery_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery_contexts = []
            review_factory = _FailOnceReviewFactory()

            def recovery_agent(context):
                recovery_contexts.append(context)
                return RecoveryDecision(
                    "retry",
                    "Retry with a fresh reviewer after the transient failure.",
                )

            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    [],
                ),
                recovery_agent=recovery_agent,
                recovery_limit=1,
                review_fix_executor_factory=review_factory,
                review_fix_policy=ReviewFixPolicy(),
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(review_factory.calls, 2)
            self.assertEqual(len(recovery_contexts), 1)
            self.assertEqual(recovery_contexts[0].stage, "review")
            self.assertEqual(recovery_contexts[0].condition, "failed")
            AuditJournal.verify(root / "run")

    @patch("harness_labs.featurerun.feature_run.ReviewFixLoop")
    def test_recovered_review_retains_transfers_from_failed_attempt(
        self, review_loop
    ) -> None:
        transferred = {
            "key": "downstream.py:required-change",
            "file": "downstream.py",
            "transferred_to": "FR-02",
            "outcome": "transferred",
        }
        review_loop.return_value.run.side_effect = (
            ReviewFixResult(
                "failed", "reviewer interrupted", 1, "mechanical", "first-ledger",
                (), (), (transferred,),
            ),
            ReviewFixResult(
                "succeeded", "review cleared", 3, "mechanical", "second-ledger",
                (), (), (),
            ),
        )
        # The controller reads these off the stopped loop to build the recovery
        # agent's stage detail and to resume the ledger.
        stopped_ledger = ReviewLedger(ReviewFixPolicy(), "mechanical")
        review_loop.return_value.ledger = stopped_ledger
        review_loop.return_value.policy = ReviewFixPolicy()
        review_loop.return_value.additional_cycles = 0
        review_loop.return_value.resume_from_cycle = 0
        review_loop.return_value.cycle_budget = (
            lambda risk_tier: ReviewFixLoop.cycle_budget(
                review_loop.return_value, risk_tier
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree, []
                ),
                recovery_agent=lambda context: RecoveryDecision(
                    "retry", "Retry the interrupted review."
                ),
                recovery_limit=1,
                review_fix_executor_factory=_FailOnceReviewFactory(),
                review_fix_policy=ReviewFixPolicy(),
            )

            self.assertEqual(result.status, "succeeded")
            self.assertIsNotNone(result.review_fix)
            self.assertEqual(result.review_fix.transferred_findings, (transferred,))
            self.assertEqual(review_loop.call_count, 2)
            # The replacement loop continues the stopped ledger, which already
            # holds the transfer, instead of re-seeding it into a cold ledger.
            recovered = review_loop.call_args_list[1].kwargs
            self.assertIs(recovered["resumed_ledger"], stopped_ledger)
            self.assertEqual(recovered["resume_from_cycle"], 1)
            self.assertEqual(
                recovered["additional_cycles"], ReviewFixPolicy().continuation_cycles
            )
            self.assertEqual(recovered["retained_transfers"], ())
            self.assertEqual(recovered["inherited_findings"], ())
            AuditJournal.verify(root / "run")

    @patch("harness_labs.featurerun.feature_run.ReviewFixLoop")
    def test_bounded_fix_only_is_wired_into_review_loop_construction(
        self, review_loop
    ) -> None:
        """CC-08: FeatureRunRequest.bounded_fix_only reaches the review loop."""

        obligation = {
            "key": "other.py:cross-node-fix",
            "file": "other.py",
            "outcome": "open",
        }
        review_loop.return_value.run.return_value = ReviewFixResult(
            "succeeded",
            "bounded fix-only cleared",
            1,
            "mechanical",
            "ledger-ref",
            (),
            (),
            (),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(worktree, []),
                review_fix_executor_factory=lambda stage, attempt: None,
                review_fix_policy=ReviewFixPolicy(),
                review_finding_obligations=(obligation,),
                review_bounded_fix_only=True,
            )
            self.assertEqual(result.status, "succeeded")
            review_loop.assert_called_once()
            construction = review_loop.call_args.kwargs
            self.assertTrue(construction["bounded_fix_only"])
            self.assertEqual(construction["seeded_fix_keys"], (obligation["key"],))
            self.assertEqual(construction["inherited_findings"], (obligation,))
            AuditJournal.verify(root / "run")

    def test_bounded_fix_only_defaults_to_false_and_no_seeded_keys(self) -> None:
        """CC-08: byte-identical wiring when a caller never opts in."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "harness_labs.featurerun.feature_run.ReviewFixLoop"
            ) as review_loop:
                review_loop.return_value.run.return_value = ReviewFixResult(
                    "succeeded", "review cleared", 1, "mechanical", "ledger-ref",
                    (), (), (),
                )
                self._run_verification_recovery_case(
                    root,
                    lambda worktree, attempt: _VerificationRepairExecutor(
                        worktree, []
                    ),
                    review_fix_executor_factory=lambda stage, attempt: None,
                    review_fix_policy=ReviewFixPolicy(),
                )
                construction = review_loop.call_args.kwargs
                self.assertFalse(construction["bounded_fix_only"])
                self.assertEqual(construction["seeded_fix_keys"], ())

    def test_bounded_fix_only_requires_finding_obligations(self) -> None:
        with self.assertRaisesRegex(ValueError, "review_bounded_fix_only"):
            run_feature_worktree(
                base_repository=Path("unused"),
                base_branch="main",
                feature_branch="feature/test",
                worktree_path=Path("unused-worktree"),
                run_dir=Path("unused-run"),
                contract_factory=lambda worktree, receipt: None,
                schema=standard_feature_run_dispatch_schema(),
                session_factory=lambda worktree, launch, evidence: None,
                profile_builder=lambda worktree, evidence: (),
                allowed_paths=("feature.txt",),
                commit_message="unused",
                review_bounded_fix_only=True,
            )

    def test_interrupted_repair_raises_recovery_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery_contexts = []

            def recovery_agent(context):
                recovery_contexts.append(context)
                (root / "feature" / "verified.txt").write_text(
                    "recovered after interruption\n",
                    encoding="utf-8",
                )
                return RecoveryDecision(
                    "retry",
                    "Resume verification after replacing the interrupted worker.",
                )

            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _InterruptedRepairExecutor(),
                recovery_agent=recovery_agent,
                recovery_limit=1,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(recovery_contexts), 1)
            self.assertEqual(recovery_contexts[0].condition, "interrupted")
            self.assertEqual(recovery_contexts[0].stage, "verification")
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [7, 0],
            )
            self.assertEqual(
                result.verification.repair_invocations,
                ({
                    "invocation_id": (
                        "feature-verification-run:verification-repair:"
                        "post_implementation:1"
                    ),
                    "classification": "product",
                    "failure_keys": [],
                },),
            )
            AuditJournal.verify(root / "run")

    def test_no_change_verification_repair_triggers_fresh_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts = []

            def repair_factory(worktree, attempt):
                class Executor:
                    def execute(self, current_attempt):
                        context = json.loads(current_attempt.context)
                        seen_contexts.append(context)
                        if len(seen_contexts) == 1:
                            return TaskResult(
                                current_attempt.attempt_id,
                                "failed",
                                {
                                    "error": (
                                        "writable worker completed without "
                                        "changing the repository"
                                    ),
                                    "error_type": "LiveExecutionError",
                                },
                            )
                        (worktree / "verified.txt").write_text(
                            "repaired\n",
                            encoding="utf-8",
                        )
                        return TaskResult(
                            current_attempt.attempt_id,
                            "succeeded",
                            {"summary": "Recovered with a changed method."},
                        )

                return Executor()

            result = self._run_verification_recovery_case(root, repair_factory)

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(result.verification.repair_attempts, 2)
            self.assertEqual(len(seen_contexts), 2)
            self.assertIsNone(seen_contexts[0].get("recovery"))
            self.assertEqual(seen_contexts[1]["recovery"]["attempt"], 1)
            events = [
                json.loads(line)
                for line in (result.run_dir / "events.jsonl").read_text().splitlines()
            ]
            triggered = [
                event
                for event in events
                if event["event_type"]
                == "deterministic_verification_recovery_triggered"
            ]
            self.assertEqual(len(triggered), 1)
            self.assertEqual(triggered[0]["status"], "recovering")
            AuditJournal.verify(result.run_dir)

    def _run_verification_recovery_case(
        self,
        root,
        repair_factory,
        **recovery_options,
    ):
        base = root / "base"
        base.mkdir()
        git(base, "init", "-b", "main")
        git(base, "config", "user.name", "Harness Tests")
        git(base, "config", "user.email", "harness@example.invalid")
        (base / "README.md").write_text("base\n", encoding="utf-8")
        git(base, "add", "README.md")
        git(base, "commit", "--no-gpg-sign", "-m", "Base")
        schema = CoordinatorDispatchSchema(
            "feature-verification-test/1",
            (
                CoordinatorSegment(
                    id="active",
                    phases=("active",),
                    instructions="Build and complete.",
                ),
            ),
        )

        def contract_factory(worktree, receipt):
            return RunContract(
                run_id="feature-verification-run",
                objective="Build and verify a file.",
                phases=("active",),
                criteria=(
                    {
                        "id": "built",
                        "statement": "The file is built and verified.",
                        "source": "operator",
                    },
                ),
                terminal_artifact_kinds=("implementation-summary",),
                repository={
                    "path": str(worktree),
                    "branch": receipt["feature_branch"],
                    "base_branch": receipt["base_branch"],
                    "base_commit": receipt["base_commit"],
                },
            )

        def session_factory(worktree, launch, evidence):
            return ScriptedCoordinatorSession(
                [
                    (
                        "task_dispatch",
                        {
                            "tasks": [
                                {
                                    "id": "build",
                                    "role": "builder",
                                    "objective": "Build feature.txt",
                                    "details_schema": "build/1",
                                    "required_capabilities": ["repo.write"],
                                    "acceptance_criteria": ["built"],
                                    "dependencies": [],
                                }
                            ],
                            "max_parallelism": 1,
                        },
                    ),
                    ("run_complete_request", {}),
                ],
                final="Complete.",
            )

        command = (
            "python3",
            "-c",
            "import pathlib,sys; p=pathlib.Path('verified.txt'); "
            "print('verification failed') if not p.exists() else None; "
            "print('missing verified.txt', file=sys.stderr) if not p.exists() else None; "
            "sys.exit(0 if p.exists() else 7)",
        )
        worktree = root / "feature"
        return run_feature_worktree(
            base_repository=base,
            base_branch="main",
            feature_branch="feature/test",
            worktree_path=worktree,
            run_dir=root / "run",
            contract_factory=contract_factory,
            schema=schema,
            session_factory=session_factory,
            profile_builder=lambda candidate, evidence: (
                RoleProfile(
                    "builder",
                    "builder",
                    frozenset({"repo.write"}),
                    lambda task: _BuildExecutor(task, candidate, evidence),
                ),
            ),
            allowed_paths=("feature.txt", "verified.txt"),
            commit_message="Build verified feature",
            merge=False,
            verification_argv=command,
            verification_repair_executor_factory=lambda attempt: repair_factory(
                worktree,
                attempt,
            ),
            verification_repair_limit=1,
            evidence_classification="component",
            **recovery_options,
        )


class DeterministicRecoveryAgentTests(unittest.TestCase):
    """Rule-based default recovery: retry transients, stop classified."""

    @staticmethod
    def _context(condition="failed", reason="", stage="implement", attempt=1):
        from harness_labs.featurerun.feature_run import RecoveryContext

        return RecoveryContext(
            run_id="run-1",
            stage=stage,
            condition=condition,
            reason=reason,
            attempt=attempt,
            checkpoint={},
            objective="objective",
            acceptance_criteria=(),
            worktree_path="/tmp/worktree",
            allowed_paths=("src",),
            workspace={},
            prior_decisions=(),
            plan_adjustments=(),
        )

    def test_interrupted_condition_retries(self) -> None:
        from harness_labs.featurerun.feature_run import deterministic_recovery_agent

        decision = deterministic_recovery_agent(
            self._context(condition="interrupted", reason="dispatcher died")
        )
        self.assertEqual(decision.action, "retry")

    def test_transient_signature_retries_with_distinct_reasons(self) -> None:
        from harness_labs.featurerun.feature_run import deterministic_recovery_agent

        first = deterministic_recovery_agent(
            self._context(reason="terminal_reason aborted_streaming", attempt=1)
        )
        second = deterministic_recovery_agent(
            self._context(reason="terminal_reason aborted_streaming", attempt=2)
        )
        self.assertEqual((first.action, second.action), ("retry", "retry"))
        self.assertNotEqual(first.reason, second.reason)

    def test_review_cycle_limit_stops_with_classification(self) -> None:
        from harness_labs.featurerun.feature_run import deterministic_recovery_agent

        decision = deterministic_recovery_agent(
            self._context(
                condition="blocked",
                stage="review_fix",
                reason="cycle limit reached",
            )
        )
        self.assertEqual(decision.action, "stop")
        self.assertIn("non-transient", decision.reason)
        self.assertIn("cycle limit reached", decision.reason)

    def test_default_is_wired_into_run_feature_worktree(self) -> None:
        import inspect

        from harness_labs.featurerun.feature_run import (
            deterministic_recovery_agent,
            run_feature_worktree,
        )

        signature = inspect.signature(run_feature_worktree)
        self.assertIs(
            signature.parameters["recovery_agent"].default,
            deterministic_recovery_agent,
        )


def _recovery_context(**overrides):
    """A review-blocked-on-cycle-limit RecoveryContext, overridable."""

    detail = {
        "stop_reason": "cycle_limit",
        "open_finding_keys": ["a.py:thing"],
        "findings_discharged": 2,
        "cycle_history": [{"cycle": 1, "addressed_finding_keys": ["a.py:thing"]}],
    }
    detail.update(overrides.pop("stage_detail", {}))
    values = {
        "run_id": "run",
        "stage": "review",
        "condition": "blocked",
        "reason": "cycle limit reached",
        "attempt": 1,
        "checkpoint": {},
        "objective": "Build it.",
        "acceptance_criteria": (),
        "worktree_path": "/tmp/worktree",
        "allowed_paths": ("a.py",),
        "workspace": {},
        "prior_decisions": (),
        "plan_adjustments": (),
        "stage_detail": detail,
    }
    values.update(overrides)
    return RecoveryContext(**values)


class ComposedRecoveryAgentTests(unittest.TestCase):
    """The platform default: continuation first, transient retry second.

    The two policies cover disjoint conditions, so binding either one alone
    silently gives up what the other handles. These pin both halves of the
    composition and the order between them.
    """

    def setUp(self) -> None:
        self.agent = standard_composed_recovery_agent()

    def test_keeps_transient_retry_from_the_deterministic_policy(self) -> None:
        for reason in (
            "terminal_reason aborted_streaming",
            "failed to lookup address information",
            "backend process terminated",
        ):
            with self.subTest(reason=reason):
                decision = self.agent(
                    _recovery_context(
                        stage="implement", condition="failed", reason=reason
                    )
                )
                self.assertEqual(decision.action, "retry")

    def test_continues_a_review_that_ran_out_of_cycles(self) -> None:
        decision = self.agent(_recovery_context())
        self.assertEqual(decision.action, "retry")
        self.assertIn("exhausted its cycle budget", decision.reason)

    def test_continuation_is_consulted_before_the_deterministic_stop(self) -> None:
        """Order is the whole design: the deterministic agent classifies a
        review block as a non-transient stop, so consulting it first would
        make the continuation unreachable."""
        from harness_labs.featurerun.feature_run import deterministic_recovery_agent

        context = _recovery_context()
        self.assertEqual(deterministic_recovery_agent(context).action, "stop")
        self.assertEqual(self.agent(context).action, "retry")

    def test_falls_through_to_deterministic_for_the_loops_own_futility(self) -> None:
        for stop_reason in ("no_progress", "marginal_yield", "required_findings_open"):
            with self.subTest(stop_reason=stop_reason):
                decision = self.agent(
                    _recovery_context(stage_detail={"stop_reason": stop_reason})
                )
                self.assertEqual(decision.action, "stop")

    def test_stops_on_a_classified_non_transient_failure(self) -> None:
        decision = self.agent(
            _recovery_context(
                stage="verification", condition="failed", reason="tests failed"
            )
        )
        self.assertEqual(decision.action, "stop")


class ReviewContinuationPolicyTests(unittest.TestCase):
    """The default agent bound to every PlanGraph-bound FeatureRun."""

    context = staticmethod(_recovery_context)

    def setUp(self) -> None:
        self.agent = standard_review_continuation_recovery_agent()

    def test_continues_a_review_that_exhausted_its_cycle_budget(self) -> None:
        decision = self.agent(self.context())
        self.assertEqual(decision.action, "retry")
        self.assertIn("exhausted its cycle budget", decision.reason)

    def test_defers_to_the_loops_own_futility_verdict(self) -> None:
        # The loop already measured that fixing was not paying off; buying it
        # more cycles would repeat a strategy it rejected.
        for stop_reason in ("no_progress", "marginal_yield", "required_findings_open"):
            with self.subTest(stop_reason=stop_reason):
                decision = self.agent(
                    self.context(stage_detail={"stop_reason": stop_reason})
                )
                self.assertEqual(decision.action, "stop")
                self.assertIn(stop_reason, decision.reason)

    def test_stops_when_no_findings_remain_open(self) -> None:
        decision = self.agent(self.context(stage_detail={"open_finding_keys": []}))
        self.assertEqual(decision.action, "stop")

    def test_stops_for_every_stage_other_than_review(self) -> None:
        for stage in ("dispatch", "verification", "integration", "post_review_verification"):
            with self.subTest(stage=stage):
                self.assertEqual(self.agent(self.context(stage=stage)).action, "stop")

    def test_stops_a_review_that_failed_rather_than_exhausted_its_cycles(self) -> None:
        # A crashed reviewer is not evidence that more cycles would help.
        decision = self.agent(self.context(condition="failed"))
        self.assertEqual(decision.action, "stop")


if __name__ == "__main__":
    unittest.main()
