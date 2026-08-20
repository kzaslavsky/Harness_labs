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

from pathlib import Path
import tempfile
import unittest

from harness_labs.graphrun import campaign_launcher
from harness_labs.graphrun.agent_mixture import build_role_profiles
from harness_labs.graphrun.escalation_judge import DEFAULT_JUDGE_IDENTITY

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


if __name__ == "__main__":
    unittest.main()
