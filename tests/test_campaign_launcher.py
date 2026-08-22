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
from unittest.mock import patch

from harness_labs.graphrun import campaign_launcher
from harness_labs.graphrun.agent_mixture import build_role_profiles
from harness_labs.graphrun.escalation_judge import DEFAULT_JUDGE_IDENTITY
from harness_labs.plangraph.plan_graph import (
    PlanGraphError,
    persist_registration,
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


# -- plan-version transition path (sanctioned amendment mechanism) -----------
TRANSITION_AUTHORITY = {
    "protocol": "plan-graph-automatic-recovery/1",
    "allowed_actions": [
        "resume",
        "extend_budget",
        "transfer_ownership",
        "revise_acceptance",
    ],
    "max_extra_node_launches": 6,
    "max_structural_decisions": 2,
}


def _transition_record(predecessor: str, successor: str) -> dict:
    return {
        "protocol": "plan-graph-version-transition/1",
        "action": "revise_acceptance",
        "predecessor_plan_sha256": predecessor,
        "successor_plan_sha256": successor,
        "node_correspondence": {"a": "a"},
        "budget_carryover": {"a": "full"},
        "authorizing_decision": {
            "protocol": "plan-graph-recovery-decision/1",
            "action": "revise_acceptance",
            "target": "plan_version",
            "expected_prior_digest": predecessor,
            "payload": {},
        },
    }


class TransitionValidationTests(unittest.TestCase):
    """load_validated_transition fails closed before any registration state."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        for argv in (
            ("init",),
            ("config", "user.email", "tests@example.com"),
            ("config", "user.name", "Tests"),
        ):
            subprocess.run(
                ["git", *argv], cwd=self.repository, check=True, capture_output=True
            )
        plan = self.repository / "docs" / "plan.md"
        plan.parent.mkdir()
        plan.write_text("plan v1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "docs/plan.md"],
            cwd=self.repository, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "plan"],
            cwd=self.repository, check=True, capture_output=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="amend-campaign",
            decomposition={
                "plan": "docs/plan.md",
                "base_commit": base,
                "runs": [
                    {
                        "id": "a",
                        "objective": "Build A",
                        "plan_sections": ["1"],
                        "criteria": ["AC-1"],
                        "depends_on": [],
                        "verification_argv": ["true"],
                    }
                ],
                "plan_sections": {"1": "Build A. AC-1: A works."},
                "acceptance_criteria": {"AC-1": "A works."},
                "functionality_tests": [],
            },
            automatic_recovery=TRANSITION_AUTHORITY,
        )
        self.registration_root = self.root / "registration"
        persist_registration(
            repository=self.repository,
            registration_root=self.registration_root,
            registration=self.registration,
        )
        self.config = campaign_launcher.build_campaign_launch_config(
            logical_graph_id="amend-campaign",
            automatic_recovery=TRANSITION_AUTHORITY,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_transition(self, record) -> Path:
        path = self.root / "transition.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_valid_transition_naming_persisted_predecessor_loads(self) -> None:
        record = _transition_record(self.registration.plan_sha256, "b" * 64)
        checked = campaign_launcher.load_validated_transition(
            self.config, self._write_transition(record), self.registration_root
        )
        self.assertEqual(checked, record)

    def test_transition_not_naming_persisted_predecessor_is_refused(self) -> None:
        record = _transition_record("f" * 64, "b" * 64)
        with self.assertRaisesRegex(PlanGraphError, "exact predecessor"):
            campaign_launcher.load_validated_transition(
                self.config, self._write_transition(record), self.registration_root
            )

    def test_transition_without_predecessor_registration_is_refused(self) -> None:
        record = _transition_record(self.registration.plan_sha256, "b" * 64)
        with self.assertRaisesRegex(PlanGraphError, "existing predecessor"):
            campaign_launcher.load_validated_transition(
                self.config,
                self._write_transition(record),
                self.root / "no-registrations",
            )

    def test_transition_without_granted_revision_action_is_refused(self) -> None:
        """The pinned default authority grants no revision action, so a
        campaign that never opted in cannot thread a transition at all."""
        config = campaign_launcher.build_campaign_launch_config(
            logical_graph_id="amend-campaign"
        )
        record = _transition_record(self.registration.plan_sha256, "b" * 64)
        with self.assertRaisesRegex(PlanGraphError, "transition is invalid"):
            campaign_launcher.load_validated_transition(
                config, self._write_transition(record), self.registration_root
            )

    def test_post_transition_plain_run_adopts_persisted_registration(self) -> None:
        """After a transition is consumed, a transition-less re-run of the
        same approved plan must adopt the persisted transition-carrying
        registration instead of failing on its embedded-transition digest."""
        plan = self.repository / "docs" / "plan.md"
        plan.write_text("plan v2\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "revise plan"],
            cwd=self.repository, check=True, capture_output=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository, check=True, capture_output=True, text=True,
        ).stdout.strip()
        revised = {
            "plan": "docs/plan.md",
            "base_commit": base,
            "runs": [
                {
                    "id": "a",
                    "objective": "Build A",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "verification_argv": ["true"],
                }
            ],
            "plan_sections": {"1": "Build A. AC-1: A works."},
            "acceptance_criteria": {"AC-1": "A works."},
            "functionality_tests": [],
        }

        def successor(transition=None):
            return register_plan_graph(
                repository=self.repository,
                logical_graph_id="amend-campaign",
                decomposition=revised,
                automatic_recovery=TRANSITION_AUTHORITY,
                plan_version_transition=transition,
            )

        transitioned = successor(
            _transition_record(
                self.registration.plan_sha256, successor().plan_sha256
            )
        )
        persisted_path = persist_registration(
            repository=self.repository,
            registration_root=self.registration_root,
            registration=transitioned,
        )
        persisted_bytes = persisted_path.read_bytes()
        with patch.object(
            campaign_launcher, "register_plan_graph", return_value=successor()
        ), patch.object(
            campaign_launcher,
            "_registration_root",
            lambda: self.registration_root,
        ):
            from types import SimpleNamespace

            adopted, adopted_path = campaign_launcher._register_campaign_graph(
                self.config,
                SimpleNamespace(
                    decomposition=revised,
                    base_commit=base,
                    repository_id="repository",
                    decomposition_path="docs/plan.md",
                ),
                None,
            )
        self.assertEqual(adopted.graph_digest, transitioned.graph_digest)
        self.assertEqual(adopted_path, persisted_path)
        self.assertEqual(persisted_path.read_bytes(), persisted_bytes)

    def test_non_object_transition_payload_is_refused(self) -> None:
        path = self.root / "transition.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(PlanGraphError, "JSON object"):
            campaign_launcher.load_validated_transition(
                self.config, path, self.registration_root
            )


class TransitionStageWiringTests(unittest.TestCase):
    """--transition threads through run/resume and is refused elsewhere."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.receipt = self.root / "receipt.json"
        self.receipt.write_text("{}", encoding="utf-8")
        self.transition_path = self.root / "transition.json"
        self.transition_path.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_stage_threads_validated_transition(self) -> None:
        sentinel = {"validated": True}
        with patch.object(
            campaign_launcher, "load_validated_transition", return_value=sentinel
        ) as load, patch.object(
            campaign_launcher, "run_graph", return_value=0
        ) as run:
            exit_code = campaign_launcher.main(
                [
                    "run",
                    "--receipt", str(self.receipt),
                    "--transition", str(self.transition_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(load.call_args.args[1], self.transition_path)
        self.assertEqual(run.call_args.kwargs["transition"], sentinel)

    def test_resume_stage_threads_validated_transition(self) -> None:
        sentinel = {"validated": True}
        with patch.object(
            campaign_launcher, "load_validated_transition", return_value=sentinel
        ), patch.object(
            campaign_launcher, "resume_graph", return_value=0
        ) as resume:
            exit_code = campaign_launcher.main(
                [
                    "resume",
                    "--receipt", str(self.receipt),
                    "--predecessor-attempt-id", "attempt-1",
                    "--blocker-evidence-ref", "artifact:sha256:" + "0" * 64,
                    "--transition", str(self.transition_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(resume.call_args.kwargs["transition"], sentinel)

    def test_refused_transition_halts_before_run(self) -> None:
        with patch.object(
            campaign_launcher,
            "load_validated_transition",
            side_effect=PlanGraphError("does not name the persisted registration"),
        ), patch.object(campaign_launcher, "run_graph", return_value=0) as run:
            exit_code = campaign_launcher.main(
                [
                    "run",
                    "--receipt", str(self.receipt),
                    "--transition", str(self.transition_path),
                ]
            )
        self.assertEqual(exit_code, 1)
        run.assert_not_called()

    def test_prepare_and_issue_refuse_transition(self) -> None:
        for stage in ("prepare", "issue"):
            with self.subTest(stage=stage), self.assertRaises(SystemExit):
                campaign_launcher.main(
                    [stage, "--transition", str(self.transition_path)]
                )

    def test_automatic_recovery_is_parameterized_with_pinned_default(self) -> None:
        config = campaign_launcher.build_campaign_launch_config(
            automatic_recovery=TRANSITION_AUTHORITY
        )
        self.assertEqual(config["automatic_recovery"], TRANSITION_AUTHORITY)
        self.assertEqual(
            campaign_launcher.build_campaign_launch_config()["automatic_recovery"],
            GOLDEN_CONFIG["automatic_recovery"],
        )


if __name__ == "__main__":
    unittest.main()
