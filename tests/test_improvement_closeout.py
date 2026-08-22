"""SI-06: recurrence and closure -- CI validation of committed improvement
artifacts, and close-out promotion of a successful fixture campaign into a
``docs/decisions/`` record the decision registry serves back.

Two independent concerns, both exercised end to end against real (never
hand-rolled) machinery:

* ``CheckerFixtureTreeTests`` (AC-SI06-2) drives the real, committed
  ``scripts/dev/check_improvement_artifacts.py`` over the seeded
  ``docs/improvement/`` tree (expect exit 0) and over a throwaway copy with
  a violation injected (expect exit 1), both via direct import and via the
  CLI subprocess.
* ``CloseOutPromotionRegistryTests`` (AC-SI06-3) drives a real fixture
  campaign -- ``harness_labs.graphrun.improvement_loop``'s
  open/synthesize/approve/dispatch/remeasure pipeline, against a throwaway
  git repository, exactly as ``tests/test_self_improve_loop.py`` proves for
  SI-05 -- to a successful close, then places the emitted decision draft
  into a tmp ``decisions_root`` (simulating an operator's acceptance) and
  asserts ``harness_labs.core.decision_registry.active_decisions_for_paths``
  serves it back for the proposal's ``target_surface`` paths. No writes
  ever land in the real ``docs/decisions/`` or ``docs/improvement/``.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.core.decision_registry import load_decisions  # noqa: E402
from harness_labs.graphrun import improvement_loop as loop  # noqa: E402
from harness_labs.plangraph.plan_approval import (  # noqa: E402
    OPERATOR_APPROVAL_PROTOCOL,
    issue_receipt,
    prepare_approval,
    warning_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = REPO_ROOT / "scripts" / "dev" / "check_improvement_artifacts.py"
IMPROVEMENT_ROOT = REPO_ROOT / "docs" / "improvement"
REAL_DECISION_TEMPLATE = REPO_ROOT / "docs" / "decisions" / "TEMPLATE.md"
LOOP_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "improvement" / "loop"
LAUNCHD_TEMPLATE = REPO_ROOT / "docs" / "operations" / "self-improve.launchd.plist.example"
AGENT_GUIDE = REPO_ROOT / "docs" / "development" / "self-improvement-agent-guide.md"
DEV_INDEX = REPO_ROOT / "docs" / "development" / "INDEX.md"

_CHECK_ARTIFACTS_SPEC = importlib.util.spec_from_file_location(
    "check_improvement_artifacts", CHECKER_PATH,
)
checker = importlib.util.module_from_spec(_CHECK_ARTIFACTS_SPEC)
_CHECK_ARTIFACTS_SPEC.loader.exec_module(checker)


# ---------------------------------------------------------------------------
# AC-SI06-1: the committed artifact home exists and is non-vacuous.
# ---------------------------------------------------------------------------


class CommittedArtifactHomeTests(unittest.TestCase):
    def test_docs_improvement_exists_and_is_seeded(self) -> None:
        self.assertTrue(IMPROVEMENT_ROOT.is_dir())
        seeded = list(IMPROVEMENT_ROOT.rglob("*.json"))
        self.assertTrue(seeded, "docs/improvement/ must seed at least one artifact")

    def test_launchd_template_exists(self) -> None:
        self.assertTrue(
            LAUNCHD_TEMPLATE.is_file(),
            "docs/operations/self-improve.launchd.plist.example must exist",
        )

    def test_agent_guide_exists(self) -> None:
        self.assertTrue(
            AGENT_GUIDE.is_file(),
            "docs/development/self-improvement-agent-guide.md must exist",
        )

    def test_agent_guide_is_registered_in_dev_index(self) -> None:
        index_text = DEV_INDEX.read_text(encoding="utf-8")
        self.assertIn(
            "self-improvement-agent-guide.md", index_text,
            "docs/development/INDEX.md must register self-improvement-agent-guide.md",
        )

    def test_logs_improvement_is_gitignored(self) -> None:
        completed = subprocess.run(
            [
                "git", "-C", str(REPO_ROOT), "check-ignore", "-q",
                "logs/improvement/patterns/does-not-exist.json",
            ],
            capture_output=True,
        )
        self.assertEqual(
            completed.returncode, 0,
            "logs/improvement/** must be covered by a .gitignore rule",
        )


# ---------------------------------------------------------------------------
# AC-SI06-2: the checker exits zero over the seeded tree, nonzero on a copy
# with a violation seeded into it.
# ---------------------------------------------------------------------------


class CheckerFixtureTreeTests(unittest.TestCase):
    def test_seeded_docs_improvement_tree_exits_zero(self) -> None:
        errors = checker.check_tree(IMPROVEMENT_ROOT)
        self.assertEqual(errors, [])

    def test_seeded_docs_improvement_tree_exits_zero_via_cli(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CHECKER_PATH), "--root", str(IMPROVEMENT_ROOT)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _copy_with_violation(self) -> Path:
        """A throwaway copy of the seeded tree with one violation injected:
        the accepted proposal's human ``ruling`` is stripped, tripping
        ``check_accepted_ruling`` (a business rule no plain schema keyword
        alone expresses)."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        copy_root = Path(temporary.name) / "docs-improvement-copy"
        shutil.copytree(IMPROVEMENT_ROOT, copy_root)

        proposal_path = copy_root / "proposals" / "proposal-gate-timeout-01.json"
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        self.assertEqual(proposal["status"], "accepted")
        del proposal["ruling"]
        proposal_path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
        return copy_root

    def test_seeded_tree_copy_with_violation_exits_nonzero(self) -> None:
        violated_root = self._copy_with_violation()
        errors = checker.check_tree(violated_root)
        self.assertTrue(errors)
        self.assertTrue(
            any("human ruling" in error for error in errors), errors,
        )

    def test_seeded_tree_copy_with_violation_exits_nonzero_via_cli(self) -> None:
        violated_root = self._copy_with_violation()
        completed = subprocess.run(
            [sys.executable, str(CHECKER_PATH), "--root", str(violated_root)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("human ruling", completed.stderr)

    def test_checker_needs_no_journal_access(self) -> None:
        """CI validates docs/improvement/ with no ``logs/runs/`` corpus
        present at all -- the checker walks only the artifact tree it is
        pointed at, so a bare copy of ``docs/improvement/`` in an otherwise
        empty directory (no ``logs/`` sibling whatsoever) still validates
        cleanly."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        isolated_root = Path(temporary.name) / "docs-improvement-isolated"
        shutil.copytree(IMPROVEMENT_ROOT, isolated_root)
        self.assertFalse((Path(temporary.name) / "logs").exists())

        errors = checker.check_tree(isolated_root)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# AC-SI06-3: close-out promotion drafts a docs/decisions/ record from
# TEMPLATE.md with Concerns-paths filled from target_surface, and once
# accepted the decision registry serves it back for those paths.
# ---------------------------------------------------------------------------


class _ClosingCampaignFixture(unittest.TestCase):
    """A real, throwaway git repository driven through the real
    open/synthesize/approve/dispatch/remeasure pipeline to a successful
    close -- the same shape ``tests/test_self_improve_loop.py``'s
    ``_RepoFixture`` proves for SI-05, reused here (not reinvented) to
    reach AC-SI06-3's close-out/registry assertions."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "si06-closeout-tests@example.com")
        self._git("config", "user.name", "SI-06 Closeout Tests")

        (self.repository / ".harness").mkdir()
        self._write_json(
            self.repository / ".harness" / "repository.json",
            {
                "protocol": "harness-repository-identity/1",
                "repository_id": "si06-closeout-test-repository",
            },
        )
        plan_dir = self.repository / "docs" / "development"
        plan_dir.mkdir(parents=True)
        (plan_dir / "self-improvement-agent-plan.md").write_text(
            "## SI-05 — Convergence bridge, loop driver, CLI [si-05-loop]\n\n"
            "Fixture plan section for tests/test_improvement_closeout.py.\n",
            encoding="utf-8",
        )
        (self.repository / "src" / "pkg_a").mkdir(parents=True)
        (self.repository / "src" / "pkg_a" / "module_a.py").write_text(
            "MARKER_A = False\n", encoding="utf-8",
        )
        (self.repository / "src" / "pkg_b").mkdir(parents=True)
        (self.repository / "src" / "pkg_b" / "module_b.py").write_text(
            "MARKER_B = False\n", encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "seed repository")

        # The "tmp decisions_root" and "fixture copy of the TEMPLATE" this
        # node's operator note requires: both live outside the throwaway
        # repository entirely, standing in for the real docs/decisions/
        # that draft_decision_record is never allowed to touch directly.
        self.decisions_root = self.root / "decisions-root"
        self.decisions_root.mkdir()
        self.template_copy = self.root / "TEMPLATE.md"
        shutil.copy(REAL_DECISION_TEMPLATE, self.template_copy)

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True, capture_output=True, check=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def _accepted_proposal_path(self) -> Path:
        proposal = json.loads(
            (LOOP_FIXTURE_ROOT / "proposal.json").read_text(encoding="utf-8")
        )
        for criterion in proposal["success_criteria"]:
            argv = criterion["assertion"]["argv"]
            if argv[0] == "python3":
                argv[0] = sys.executable
        path = self.root / "proposal.json"
        self._write_json(path, proposal)
        return path

    def _write_pattern(self, pattern_id: str, *, status: str) -> Path:
        path = self.repository / "logs" / "improvement" / "patterns" / f"{pattern_id}.json"
        self._write_json(path, {
            "protocol": "blocker-pattern/1",
            "pattern_id": pattern_id,
            "signature": "fixture-signature",
            "classification": "fixture",
            "status": status,
        })
        return path

    def _approve_round(self, round: "loop.SynthesizedRound") -> Path:
        approval_dir = round.round_dir / "approval"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=round.decomposition_path,
            output_directory=approval_dir,
        )
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "fixture: acknowledged"}
            for warning in prepared.warnings
            if warning.get("severity") == "high"
        ]
        operator_approval_path = approval_dir / "operator-approval.json"
        payload = {
            "protocol": OPERATOR_APPROVAL_PROTOCOL,
            "subject_sha256": prepared.subject_sha256,
            "actor": "operator",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "statement": "fixture approval for SI-06 closeout tests",
        }
        if acknowledgements:
            payload["warning_acknowledgements"] = acknowledgements
        self._write_json(operator_approval_path, payload)
        receipt_path = approval_dir / "receipt.json"
        issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator_approval_path,
            receipt_path=receipt_path,
        )
        return receipt_path

    def _close_a_successful_campaign(self) -> "loop.RemeasureOutcome":
        """Drive open -> round -> approve -> dispatch(stub) -> remeasure to
        a successful close, with the emitter (``draft_decision_record``,
        called from ``remeasure``'s success branch) pointed at this
        fixture's tmp ``decisions_root`` and template copy."""

        self._write_pattern("pattern-si05-fixture-01", status="proposed")
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)

        def _stub_launch_writing_fix(request: "loop.RoundLaunchRequest") -> "loop.RoundLaunchResult":
            (request.repository / "src" / "pkg_a" / "module_a.py").write_text(
                "MARKER_A = True\n", encoding="utf-8",
            )
            (request.repository / "src" / "pkg_b" / "module_b.py").write_text(
                "MARKER_B = True\n", encoding="utf-8",
            )
            return loop.RoundLaunchResult(success=True, detail={"stub": "fix-applied"})

        dispatch_outcome = loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round,
            launch=_stub_launch_writing_fix,
        )
        self.assertTrue(dispatch_outcome.success)

        outcome = loop.remeasure(
            repository=self.repository, campaign_root=opened.root,
            decisions_root=self.decisions_root, decision_template_path=self.template_copy,
        )
        self.assertEqual(outcome.lifecycle, loop.LIFECYCLE_SUCCEEDED)
        self.assertIsNotNone(outcome.decision_draft_path)
        return outcome


class CloseOutPromotionRegistryTests(_ClosingCampaignFixture):
    def test_close_out_drafts_a_record_with_concerns_paths_from_target_surface(self) -> None:
        outcome = self._close_a_successful_campaign()
        draft_path = Path(outcome.decision_draft_path)
        self.assertTrue(draft_path.is_file())
        # Drafted under the campaign root, never directly into the tmp
        # decisions_root -- the operator-acceptance step below is what
        # moves it there, exactly as a real operator's hand-copy would.
        self.assertNotIn(str(self.decisions_root), str(draft_path))

        draft_text = draft_path.read_text(encoding="utf-8")
        self.assertIn(
            "Concerns-paths: src/pkg_a/module_a.py, src/pkg_b/module_b.py", draft_text,
        )
        self.assertIn("Status: proposed", draft_text)
        self.assertEqual(list(self.decisions_root.iterdir()), [])

    def test_accepted_draft_is_served_back_by_the_decision_registry_for_its_paths(
        self,
    ) -> None:
        outcome = self._close_a_successful_campaign()
        draft_path = Path(outcome.decision_draft_path)

        # Simulate operator acceptance: hand-copy the reviewed draft into
        # the real decisions tree (here, the tmp decisions_root standing in
        # for it) and flip its Status header, exactly as
        # self-improvement-agent-guide.md's close-out section describes --
        # this test never writes into the real docs/decisions/.
        accepted_path = self.decisions_root / draft_path.name
        accepted_text = draft_path.read_text(encoding="utf-8").replace(
            "Status: proposed", "Status: accepted", 1,
        )
        accepted_path.write_text(accepted_text, encoding="utf-8")

        registry = load_decisions(self.decisions_root)
        decision_id = draft_path.stem

        for target_path in ("src/pkg_a/module_a.py", "src/pkg_b/module_b.py"):
            with self.subTest(target_path=target_path):
                result = registry.active_decisions_for_paths((target_path,))
                active_ids = {decision.id for decision in result.active}
                self.assertIn(decision_id, active_ids)
                self.assertEqual(result.inconsistencies, ())

        # A path the proposal never targeted is not governed by this record.
        unrelated_result = registry.active_decisions_for_paths(("src/pkg_c/unrelated.py",))
        self.assertNotIn(decision_id, {decision.id for decision in unrelated_result.active})

    def test_draft_left_unaccepted_is_not_served_by_the_registry(self) -> None:
        """A drafted-but-not-yet-accepted record (``Status: proposed``)
        must never be treated as active -- the human-acceptance gate is
        real, not a formality this loop could bypass by drafting alone."""

        outcome = self._close_a_successful_campaign()
        draft_path = Path(outcome.decision_draft_path)

        unaccepted_path = self.decisions_root / draft_path.name
        shutil.copy(draft_path, unaccepted_path)

        registry = load_decisions(self.decisions_root)
        result = registry.active_decisions_for_paths(("src/pkg_a/module_a.py",))
        self.assertNotIn(draft_path.stem, {decision.id for decision in result.active})

    def test_promoted_pattern_and_landing_commit_back_the_same_close(self) -> None:
        outcome = self._close_a_successful_campaign()
        self.assertIn("pattern-si05-fixture-01", outcome.promoted_pattern_ids)
        head = self._git("rev-parse", "HEAD")
        draft_text = Path(outcome.decision_draft_path).read_text(encoding="utf-8")
        self.assertIn(f"Valid-from-commit: {head}", draft_text)


if __name__ == "__main__":
    unittest.main()
