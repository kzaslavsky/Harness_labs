"""Tests for the extracted campaign launcher kit (DTR-F5 / DTR-LK-KIT).

Covers AC-LK-1 (build_campaign_launch_config pinned against a literal golden
dict transcribing experiments/run_convergence_plan_graph.py at base
8a13917), AC-LK-2 (operator notes fold into implementer/review/fix but not
verify instructions), AC-LK-3 (the shared ANTI_PLACEHOLDER_FLOOR constant is
a documented widening present in all four worker instructions), AC-LK-4 (the
experiments script is a thin shim: run_plan_graph_feature_worktree no longer
appears in it, and the built config equals the golden dict), and AC-LK-8
(CC-08 wiring per ADR 0007: escalation_judge seat, transfer_ownership in
automatic_recovery.allowed_actions, max_structural_decisions bound).
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from harness_labs.featurerun.feature_run import (
    DeterministicVerificationResult,
    FeatureRunResult,
)
from harness_labs.featurerun.review_fix import ReviewFixResult
from harness_labs.graphrun import campaign_launcher
from harness_labs.graphrun.agent_mixture import build_role_profiles
from harness_labs.graphrun.escalation_judge import (
    DEFAULT_JUDGE_IDENTITY,
    ConfirmEverythingStubJudge,
)
from harness_labs.plangraph.plan_graph import (
    FEATURE_RUN_REQUEST_PROTOCOL,
    FeatureRunRequest,
    PlanGraph,
    PlanRun,
    register_plan_graph,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM_PATH = REPO_ROOT / "experiments" / "run_convergence_plan_graph.py"

# The literal golden dict transcribing
# experiments/run_convergence_plan_graph.py at base 8a13917, plus the two
# documented widenings (ANTI_PLACEHOLDER_FLOOR is not itself a config-surface
# value; the escalation_judge seat and automatic_recovery entries are).
GOLDEN_CONFIG = {
    "coordinator": {
        "spec": "claude:claude-opus-4-8[1m]@medium",
        "timeout_seconds": 7200.0,
    },
    "implementer": {
        "spec": "claude:claude-sonnet-5@high",
        "model": "claude-sonnet-5",
    },
    "reviewer": {
        "model": "claude-opus-5",
    },
    "recovery_limit": 5,
    "continuation_recovery_limit": 3,
    "verification_repair_limit": 3,
    "allow_dirty_baseline": True,
    "require_repository_change": True,
    "candidate_only": True,
    "merge": False,
    "max_parallelism": 5,
    "plan_path": "docs/development/convergence-campaign-plan.md",
    "decomposition_path": "docs/development/convergence-campaign-decomposition.json",
    "logical_graph_id": "convergence-campaign-harness",
    "agent_mixture": {"convergence_implementer": "claude:claude-sonnet-5@high"},
    "profile_builder_hook": build_role_profiles,
    "operator_notes_dir": "logs/plan-approval/operator-notes",
    "escalation_judge": {
        "identity": DEFAULT_JUDGE_IDENTITY,
        "spec": "claude:claude-opus-4-8[1m]@medium",
        "timeout_seconds": 900.0,
    },
    "automatic_recovery": {
        "protocol": "plan-graph-automatic-recovery/1",
        "allowed_actions": ("resume", "extend_budget", "transfer_ownership"),
        "max_extra_node_launches": 6,
        "max_structural_decisions": 2,
    },
}


# -- AC-LK-1 / AC-LK-4: golden-dict pin --------------------------------------


class BuildCampaignLaunchConfigTests(unittest.TestCase):
    def test_matches_golden_dict_transcribed_from_base_8a13917(self) -> None:
        self.assertEqual(campaign_launcher.build_campaign_launch_config(), GOLDEN_CONFIG)

    def test_coordinator_timeout_is_silence_tolerance_7200(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertEqual(config["coordinator"]["timeout_seconds"], 7200.0)

    def test_worktree_policy_booleans(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertIs(config["allow_dirty_baseline"], True)
        self.assertIs(config["require_repository_change"], True)
        self.assertIs(config["candidate_only"], True)
        self.assertIs(config["merge"], False)

    def test_recovery_limits(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertEqual(config["recovery_limit"], 5)
        self.assertEqual(config["continuation_recovery_limit"], 3)
        self.assertEqual(config["verification_repair_limit"], 3)

    def test_max_parallelism_five(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertEqual(config["max_parallelism"], 5)

    def test_operator_notes_dir_is_parameterized(self) -> None:
        default_config = campaign_launcher.build_campaign_launch_config()
        self.assertEqual(
            default_config["operator_notes_dir"],
            "logs/plan-approval/operator-notes",
        )
        overridden = campaign_launcher.build_campaign_launch_config(
            operator_notes_dir="some/other/notes/dir"
        )
        self.assertEqual(overridden["operator_notes_dir"], "some/other/notes/dir")

    def test_product_values_are_parameterized(self) -> None:
        overridden = campaign_launcher.build_campaign_launch_config(
            plan_path="docs/development/other-plan.md",
            decomposition_path="docs/development/other-decomposition.json",
            logical_graph_id="other-graph",
            agent_mixture={"other_implementer": "claude:claude-sonnet-5@high"},
        )
        self.assertEqual(overridden["plan_path"], "docs/development/other-plan.md")
        self.assertEqual(
            overridden["decomposition_path"],
            "docs/development/other-decomposition.json",
        )
        self.assertEqual(overridden["logical_graph_id"], "other-graph")
        self.assertEqual(
            overridden["agent_mixture"],
            {"other_implementer": "claude:claude-sonnet-5@high"},
        )
        # Non-overridden values still transcribe the source verbatim.
        self.assertEqual(
            overridden["coordinator"], GOLDEN_CONFIG["coordinator"]
        )


# -- AC-LK-2: operator notes fold into implementer/review/fix, not verify ---


class OperatorNoteFoldingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.notes_dir = Path(self.temporary.name) / "operator-notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.node_id = "dtr-lk-kit"
        self.marker = "UNIQUE-OPERATOR-RELIEF-MARKER-7f3c2a"
        (self.notes_dir / f"{self.node_id}.md").write_text(
            self.marker, encoding="utf-8"
        )

    def test_note_present_in_implementer_instructions(self) -> None:
        instructions = campaign_launcher.implementer_instructions(
            node_id=self.node_id,
            objective="do the thing",
            plan_sections=("dtr-lk",),
            allowed_paths=("harness_labs/graphrun/campaign_launcher.py",),
            verification_argv=("python3", "-m", "pytest", "-q"),
            plan_path="docs/development/delta-to-run-plan.md",
            decomposition_path="docs/development/delta-to-run-decomposition.json",
            operator_notes_dir=self.notes_dir,
        )
        self.assertIn(self.marker, instructions)

    def test_note_present_in_review_instructions(self) -> None:
        instructions = campaign_launcher.review_instructions(
            node_id=self.node_id,
            plan_path="docs/development/delta-to-run-plan.md",
            decomposition_path="docs/development/delta-to-run-decomposition.json",
            operator_notes_dir=self.notes_dir,
        )
        self.assertIn(self.marker, instructions)

    def test_note_present_in_fix_instructions(self) -> None:
        instructions = campaign_launcher.fix_instructions(
            node_id=self.node_id,
            writable_paths=("harness_labs/graphrun/campaign_launcher.py",),
            verification_argv=("python3", "-m", "pytest", "-q"),
            operator_notes_dir=self.notes_dir,
        )
        self.assertIn(self.marker, instructions)

    def test_note_absent_from_verify_instructions(self) -> None:
        # verify_instructions takes no operator_notes_dir argument at all:
        # the source launcher never folds notes into the verify stage.
        instructions = campaign_launcher.verify_instructions(
            verification_argv=("python3", "-m", "pytest", "-q"),
        )
        self.assertNotIn(self.marker, instructions)

    def test_note_folded_in_all_three_and_absent_from_verify_together(self) -> None:
        implementer = campaign_launcher.implementer_instructions(
            node_id=self.node_id,
            objective="do the thing",
            plan_sections=("dtr-lk",),
            allowed_paths=("harness_labs/graphrun/campaign_launcher.py",),
            verification_argv=("python3", "-m", "pytest", "-q"),
            plan_path="docs/development/delta-to-run-plan.md",
            decomposition_path="docs/development/delta-to-run-decomposition.json",
            operator_notes_dir=self.notes_dir,
        )
        review = campaign_launcher.review_instructions(
            node_id=self.node_id,
            plan_path="docs/development/delta-to-run-plan.md",
            decomposition_path="docs/development/delta-to-run-decomposition.json",
            operator_notes_dir=self.notes_dir,
        )
        fix = campaign_launcher.fix_instructions(
            node_id=self.node_id,
            writable_paths=("harness_labs/graphrun/campaign_launcher.py",),
            verification_argv=("python3", "-m", "pytest", "-q"),
            operator_notes_dir=self.notes_dir,
        )
        verify = campaign_launcher.verify_instructions(
            verification_argv=("python3", "-m", "pytest", "-q"),
        )
        for label, text in (("implementer", implementer), ("review", review), ("fix", fix)):
            self.assertIn(self.marker, text, f"operator note missing from {label} instructions")
        self.assertNotIn(self.marker, verify)


# -- AC-LK-3: single shared ANTI_PLACEHOLDER_FLOOR in all four instructions -


class AntiPlaceholderFloorTests(unittest.TestCase):
    def test_floor_is_a_single_shared_module_constant(self) -> None:
        self.assertIsInstance(campaign_launcher.ANTI_PLACEHOLDER_FLOOR, str)
        self.assertGreater(len(campaign_launcher.ANTI_PLACEHOLDER_FLOOR), 0)

    def test_floor_present_in_all_four_worker_instructions(self) -> None:
        floor = campaign_launcher.ANTI_PLACEHOLDER_FLOOR
        implementer = campaign_launcher.implementer_instructions(
            node_id="dtr-lk-kit",
            objective="do the thing",
            plan_sections=("dtr-lk",),
            allowed_paths=("harness_labs/graphrun/campaign_launcher.py",),
            verification_argv=("python3", "-m", "pytest", "-q"),
            plan_path="docs/development/delta-to-run-plan.md",
            decomposition_path="docs/development/delta-to-run-decomposition.json",
            operator_notes_dir="logs/plan-approval/operator-notes",
        )
        review = campaign_launcher.review_instructions(
            node_id="dtr-lk-kit",
            plan_path="docs/development/delta-to-run-plan.md",
            decomposition_path="docs/development/delta-to-run-decomposition.json",
            operator_notes_dir="logs/plan-approval/operator-notes",
        )
        fix = campaign_launcher.fix_instructions(
            node_id="dtr-lk-kit",
            writable_paths=("harness_labs/graphrun/campaign_launcher.py",),
            verification_argv=("python3", "-m", "pytest", "-q"),
            operator_notes_dir="logs/plan-approval/operator-notes",
        )
        verify = campaign_launcher.verify_instructions(
            verification_argv=("python3", "-m", "pytest", "-q"),
        )
        for label, text in (
            ("implementer", implementer),
            ("fix", fix),
            ("review", review),
            ("verify", verify),
        ):
            self.assertIn(floor, text, f"ANTI_PLACEHOLDER_FLOOR missing from {label} instructions")


# -- AC-LK-4: thin shim ------------------------------------------------------


class ThinShimTests(unittest.TestCase):
    def test_run_plan_graph_feature_worktree_absent_from_shim_source(self) -> None:
        source = SHIM_PATH.read_text(encoding="utf-8")
        self.assertNotIn("run_plan_graph_feature_worktree", source)

    def test_shim_delegates_to_campaign_launcher(self) -> None:
        source = SHIM_PATH.read_text(encoding="utf-8")
        self.assertIn("harness_labs.graphrun.campaign_launcher", source)

    def test_built_config_equals_golden_dict(self) -> None:
        # Parity is scoped to the config surface (AC-LK-1); the two
        # documented widenings (ANTI_PLACEHOLDER_FLOOR, CC-08 escalation
        # wiring) are outside that scope by construction, and are already
        # part of GOLDEN_CONFIG's escalation_judge/automatic_recovery
        # entries rather than a separate comparison.
        self.assertEqual(campaign_launcher.build_campaign_launch_config(), GOLDEN_CONFIG)


# -- AC-LK-8: CC-08 wiring per ADR 0007 --------------------------------------


class Cc08WideningTests(unittest.TestCase):
    def test_config_carries_escalation_judge_seat(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertIn("escalation_judge", config)
        judge = config["escalation_judge"]
        self.assertEqual(judge["identity"], DEFAULT_JUDGE_IDENTITY)
        self.assertIsInstance(judge["spec"], str)
        self.assertIsInstance(judge["timeout_seconds"], float)

    def test_transfer_ownership_in_automatic_recovery_allowed_actions(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertIn(
            "transfer_ownership", config["automatic_recovery"]["allowed_actions"]
        )

    def test_max_structural_decisions_bound_present(self) -> None:
        config = campaign_launcher.build_campaign_launch_config()
        self.assertIn("max_structural_decisions", config["automatic_recovery"])
        self.assertIsInstance(
            config["automatic_recovery"]["max_structural_decisions"], int
        )
        self.assertGreater(
            config["automatic_recovery"]["max_structural_decisions"], 0
        )


# -- evidence seam: _launch_node must hand back outcome_evidence() -----------


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def _escalated_record(key: str, required_paths: list[str], protects: str) -> dict:
    """A full escalated-finding record as the review-fix ledger emits it."""

    return {
        "key": key,
        "file": required_paths[0],
        "anchor_path": required_paths[0],
        "line": None,
        "end_line": None,
        "subject": "out of grant",
        "statement": "This finding needs a path outside my grant.",
        "category": "review",
        "severity": "critical",
        "score": 90,
        "fix_cost": "local",
        "protects": protects,
        "requires_disposition": True,
        "contract_violation": False,
        "scope_expanding": True,
        "outcome": "escalated",
        "outcome_reason": "escalated: required_paths_outside_grant",
        "escalation_reason": "required_paths_outside_grant",
        "cycles_seen": [1],
        "occurrences": 1,
        "source_finding_ids": [key],
        "evidence_refs": [],
        "fix_attempts": [],
        "reopened_count": 0,
        "origin_node": "",
        "transferred_to": "",
        "transfer_eligible": True,
        "required_paths": list(required_paths),
        "anchor_out_of_grant": False,
        "scope_screen_class": "",
        "inherited": False,
    }


def _review_fix_result(*escalated: dict) -> ReviewFixResult:
    return ReviewFixResult(
        status="completed",
        reason="review-fix loop completed",
        cycles=1,
        risk_tier="standard",
        ledger_ref="artifact:sha256:" + "0" * 64,
        open_finding_keys=(),
        technical_debt_keys=(),
        escalated_findings=tuple(escalated),
    )


def _stub_feature_run_result(
    node_id: str, run_dir: Path, review_fix: ReviewFixResult | None
) -> FeatureRunResult:
    verification = DeterministicVerificationResult(
        status="succeeded",
        reason="verification command exited 0",
        command_attempts=(
            {
                "invocation_id": f"verify-{node_id}-1",
                "argv": ["python3", "-m", "pytest", "-q"],
                "exit_code": 0,
            },
        ),
        repair_attempts=0,
    )
    return FeatureRunResult(
        status="succeeded",
        contract=None,
        dispatch=None,
        run_view={},
        git_receipts=(
            {"operation": "commit", "candidate_commit": f"{node_id}-commit"},
        ),
        manifest={},
        run_dir=run_dir,
        worktree_path=run_dir,
        review_fix=review_fix,
        verification=verification,
    )


class LaunchNodeOutcomeEvidenceTests(unittest.TestCase):
    """The launcher must return the canonical ``outcome_evidence()`` shape.

    A launcher that hand-builds ``{"verification": ...}`` only starves
    ``PlanGraph._escalated_findings`` / ``_transferred_findings`` /
    ``_carried_finding_obligations`` on every node, so escalations never
    reach the CC-08 judge and open findings are rediscovered from scratch
    on retry (observed empirically on si-graph-r2 attempts 3-5).
    """

    def test_outcome_carries_review_fix_and_verification_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            run_dir.mkdir()
            run = PlanRun(
                id="node-1",
                objective="Build node 1",
                plan_sections=("1",),
                criteria=("AC-1",),
                verification_argv=("python3", "-m", "pytest", "-q"),
                allowed_paths=("consumer.py",),
            )
            request = FeatureRunRequest(
                protocol=FEATURE_RUN_REQUEST_PROTOCOL,
                run=run,
                base_commit="0" * 40,
                plan="docs/approved-plan.md",
                plan_base_commit="0" * 40,
                plan_sha256="0" * 64,
                plan_graph_id="graph-1",
                plan_node_id="node-1",
                feature_run_id="fr-node-1",
                run_dir=run_dir,
            )
            record = _escalated_record(
                "consumer.py:needs-producer", ["producer.py"], protects="AC-1"
            )
            result = _stub_feature_run_result(
                "node-1", run_dir, _review_fix_result(record)
            )
            config = campaign_launcher.build_campaign_launch_config()

            with mock.patch.object(
                campaign_launcher,
                "run_plan_graph_feature_worktree",
                return_value=result,
            ):
                outcome = campaign_launcher._launch_node(
                    config, request, {"AC-1": "A works."}, "main"
                )

            self.assertEqual(outcome.status, "succeeded")
            self.assertEqual(outcome.candidate_commit, "node-1-commit")
            # The full canonical shape, not a hand-built verification-only dict.
            self.assertEqual(outcome.evidence, result.outcome_evidence())
            escalated = outcome.evidence["review_fix"]["escalated_findings"]
            self.assertEqual(len(escalated), 1)
            self.assertEqual(escalated[0]["key"], "consumer.py:needs-producer")
            self.assertEqual(
                escalated[0]["escalation_reason"], "required_paths_outside_grant"
            )
            self.assertEqual(
                [
                    attempt["invocation_id"]
                    for attempt in outcome.evidence["verification"]["command_attempts"]
                ],
                ["verify-node-1-1"],
            )

    def test_outcome_evidence_is_none_when_run_produced_none(self) -> None:
        """``outcome_evidence() or None`` — an evidence-free run must keep the
        prior ``evidence=None`` contract rather than hand the graph ``{}``."""

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            run_dir.mkdir()
            run = PlanRun(
                id="node-1",
                objective="Build node 1",
                plan_sections=("1",),
                criteria=("AC-1",),
                verification_argv=("python3", "-m", "pytest", "-q"),
                allowed_paths=("consumer.py",),
            )
            request = FeatureRunRequest(
                protocol=FEATURE_RUN_REQUEST_PROTOCOL,
                run=run,
                base_commit="0" * 40,
                plan="docs/approved-plan.md",
                plan_base_commit="0" * 40,
                plan_sha256="0" * 64,
                plan_graph_id="graph-1",
                plan_node_id="node-1",
                feature_run_id="fr-node-1",
                run_dir=run_dir,
            )
            result = _stub_feature_run_result("node-1", run_dir, None)
            result = FeatureRunResult(
                status=result.status,
                contract=None,
                dispatch=None,
                run_view={},
                git_receipts=result.git_receipts,
                manifest={},
                run_dir=run_dir,
                worktree_path=run_dir,
                review_fix=None,
                verification=None,
            )
            config = campaign_launcher.build_campaign_launch_config()

            with mock.patch.object(
                campaign_launcher,
                "run_plan_graph_feature_worktree",
                return_value=result,
            ):
                outcome = campaign_launcher._launch_node(
                    config, request, {"AC-1": "A works."}, "main"
                )

            self.assertIsNone(outcome.evidence)


class LauncherEscalationIntegrationTests(unittest.TestCase):
    """Escalations flow launcher -> graph -> CC-08 judge -> unseal.

    Drives PlanGraph through the real ``_launcher`` wrapper (only the inner
    ``run_plan_graph_feature_worktree`` is stubbed) with
    ``ConfirmEverythingStubJudge``, mirroring tests/test_plan_graph.py's
    stub-launcher escalation pin: a ``plan_graph_escalation_judged`` event is
    journaled and the sealed owner node is unsealed.  Under the old
    verification-only evidence dict both were unreachable.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        _git(self.repository, "init")
        _git(self.repository, "config", "user.email", "tests@example.com")
        _git(self.repository, "config", "user.name", "Tests")
        plan = self.repository / "docs" / "approved-plan.md"
        plan.parent.mkdir()
        plan.write_text("Approved PlanGraph plan\n", encoding="utf-8")
        _git(self.repository, "add", "docs/approved-plan.md")
        _git(self.repository, "commit", "-m", "approved plan")
        self.base_commit = _git(self.repository, "rev-parse", "HEAD")
        self.payload = {
            "plan": "docs/approved-plan.md",
            "base_commit": self.base_commit,
            "runs": [
                {
                    "id": "a", "objective": "Build A", "plan_sections": ["1"],
                    "criteria": ["AC-1"], "depends_on": [],
                    "verification_argv": ["python3", "-m", "unittest"],
                    "allowed_paths": ["producer.py"],
                },
                {
                    "id": "b", "objective": "Build B", "plan_sections": ["2"],
                    "criteria": ["AC-2"], "depends_on": ["a"],
                    "verification_argv": ["python3", "-m", "unittest"],
                    "allowed_paths": ["consumer.py"],
                },
            ],
            "plan_sections": {
                "1": "Build A. AC-1: A works.",
                "2": "Build B. AC-2: B works.",
            },
            "acceptance_criteria": {"AC-1": "A works.", "AC-2": "B works."},
            "functionality_tests": [],
        }
        (self.root / "decomposition.json").write_text(
            json.dumps(self.payload), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_escalation_reaches_judge_and_unseals_owner(self) -> None:
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="launcher-evidence-seam",
            decomposition=self.payload,
            automatic_recovery={
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": ["transfer_ownership"],
                "max_extra_node_launches": 0,
                "max_structural_decisions": 1,
            },
        )
        record = _escalated_record(
            "consumer.py:needs-producer", ["producer.py"], protects="AC-2"
        )

        def stub_run(**kwargs):
            binding = kwargs["binding"]
            run_dir = kwargs["run_dir"]
            review_fix = (
                _review_fix_result(record)
                if binding.plan_node_id == "b"
                else _review_fix_result()
            )
            return _stub_feature_run_result(
                binding.plan_node_id, run_dir, review_fix
            )

        config = campaign_launcher.build_campaign_launch_config(
            decomposition_path="decomposition.json"
        )
        judge = ConfirmEverythingStubJudge()

        with mock.patch.object(campaign_launcher, "ROOT", self.root), \
                mock.patch.object(
                    campaign_launcher,
                    "run_plan_graph_feature_worktree",
                    side_effect=stub_run,
                ):
            launcher = campaign_launcher._launcher(config, "main")
            graph = PlanGraph(
                self.repository,
                registration,
                launcher,
                run_root=self.root / "runs",
                graph_run_id="launcher-evidence-attempt-1",
                escalation_judge=judge,
            )
            result = graph.run()

        # Confirmed escalation: owner "a" unsealed, attempt blocked for repair.
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failed_run_id, "b")
        self.assertEqual(len(judge.packets), 1)

        audit = graph._audit_for_run()
        events = [
            json.loads(line)
            for line in audit.journal.events_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        judged = [
            event for event in events
            if event["event_type"] == "plan_graph_escalation_judged"
        ]
        self.assertEqual(len(judged), 1)
        self.assertEqual(judged[0]["payload"]["finding_key"], record["key"])
        self.assertEqual(judged[0]["payload"]["verdict"], "confirm")

        unsealed = [
            event for event in events
            if event["event_type"] == "plan_graph_node_unsealed"
        ]
        self.assertEqual(len(unsealed), 1)
        self.assertEqual(unsealed[0]["payload"]["plan_node_id"], "a")
        self.assertEqual(unsealed[0]["payload"]["origin_node"], "b")
        self.assertEqual(unsealed[0]["payload"]["finding_key"], record["key"])

        escalation = json.loads(
            (self.root / "runs" / graph.graph_run_id / "escalation.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(len(escalation["escalations"]), 1)
        self.assertEqual(escalation["escalations"][0]["owner_node"], "a")
        self.assertEqual(escalation["escalations"][0]["origin_node"], "b")


if __name__ == "__main__":
    unittest.main()
