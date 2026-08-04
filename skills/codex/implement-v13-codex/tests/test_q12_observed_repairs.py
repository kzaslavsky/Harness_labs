from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from resolve_required_doc_link import resolve  # noqa: E402
from run_feature import COORDINATOR_JUDGMENT_REASONS, _coordinator_limits  # noqa: E402
from state_io import StateError  # noqa: E402
from validate_plan_decision_link import validate  # noqa: E402


class Q12ObservedRepairTests(unittest.TestCase):
    def test_group3_bounds_judgment_without_inventing_benchmark_limits(self) -> None:
        self.assertEqual(
            COORDINATOR_JUDGMENT_REASONS,
            {
                "novel_contract_choice",
                "ambiguous_dependency_decomposition",
                "semantic_conflict_resolution",
                "integration_risk_judgment",
            },
        )
        self.assertIsNone(_coordinator_limits({}))
        contract = (
            Path(__file__).resolve().parents[1] / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("benchmarks never create defaults", contract)
        self.assertIn("routine redesign, retry, configured escalation", contract)

    def test_parent_contract_uses_checkpoint_after_unexplained_55_seconds(self) -> None:
        package = Path(__file__).resolve().parents[1]
        serial = package.parent / "serial-implement-codex"
        contract = (serial / "SKILL.md").read_text(encoding="utf-8")
        protocol = (serial / "references/protocol.md").read_text(encoding="utf-8")
        self.assertIn("liveness, never that planning is still active", contract)
        self.assertIn("wait QUEUE --timeout-seconds 0", contract)
        self.assertIn("phase unknown", contract)
        self.assertIn("sole phase authority", protocol)

    def test_historical_link_recovers_one_feature_keyed_archived_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            document = repo / "docs/development/decisions/2026-07-q11-decisions.md"
            archived = repo / "docs/archive/2026-07/q11-durable-plan.md"
            document.parent.mkdir(parents=True)
            archived.parent.mkdir(parents=True)
            document.write_text("[Plan](../2026-07-q11-implementation-plan.md)\n", encoding="utf-8")
            archived.write_text("# Q11 plan\n", encoding="utf-8")
            result = resolve(repo, document, "../2026-07-q11-implementation-plan.md", apply=True)
            self.assertEqual(result["replacement"], "../../archive/2026-07/q11-durable-plan.md")
            self.assertIn(result["replacement"], document.read_text(encoding="utf-8"))

    def test_historical_link_blocks_zero_or_ambiguous_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            document = repo / "docs/development/q11.md"
            document.parent.mkdir(parents=True)
            document.write_text("[Plan](missing-q11-plan.md)\n", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "0 candidate"):
                resolve(repo, document, "missing-q11-plan.md")
            archive = repo / "docs/archive"
            archive.mkdir(parents=True)
            (archive / "q11-one-plan.md").write_text("one", encoding="utf-8")
            (archive / "q11-two-plan.md").write_text("two", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "2 candidate"):
                resolve(repo, document, "missing-q11-plan.md")

    def test_plan_must_link_exact_recorded_decision_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "docs/archive/2026-07/q12-plan.md"
            decision = root / "docs/development/decisions/q12.md"
            plan.parent.mkdir(parents=True)
            decision.parent.mkdir(parents=True)
            decision.write_text("# Decisions\n", encoding="utf-8")
            plan.write_text("# Plan\n", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "does not link"):
                validate(plan, decision)
            plan.write_text("[Decisions](../../development/decisions/q12.md)\n", encoding="utf-8")
            self.assertEqual(validate(plan, decision)["status"], "valid")


if __name__ == "__main__":
    unittest.main()
