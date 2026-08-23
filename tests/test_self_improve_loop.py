"""SI-05: end-to-end tests for the convergence bridge / loop driver.

No live model, no real browser: ``round``'s PlanGraph dispatch is stubbed
through the injectable ``launch`` callable, and every assertion in the
fixture proposal is a deterministic ``argv`` subprocess check. Everything
else -- ``ConvergenceLedger``, ``plan_synthesis``, and the real
``plan_approval.prepare_approval``/``issue_receipt`` admission gates -- runs
for real against an isolated, throwaway git repository (never the actual
harness_labs checkout).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.graphrun import improvement_loop as loop  # noqa: E402
from harness_labs.graphrun.improvement_program import (  # noqa: E402
    ProposalDraft,
    SuccessCriterionDraft,
    TargetSurfaceDraft,
)
from harness_labs.plangraph.plan_approval import (  # noqa: E402
    OPERATOR_APPROVAL_PROTOCOL,
    issue_receipt,
    prepare_approval,
    warning_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "improvement" / "loop"
CORPUS_ROOT = REPO_ROOT / "tests" / "fixtures" / "improvement" / "journals" / "corpus"
REAL_DECISION_TEMPLATE = REPO_ROOT / "docs" / "decisions" / "TEMPLATE.md"

_LANDING_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

_SELF_IMPROVE_SPEC = importlib.util.spec_from_file_location(
    "self_improve", REPO_ROOT / "scripts" / "self_improve.py"
)
self_improve = importlib.util.module_from_spec(_SELF_IMPROVE_SPEC)
_SELF_IMPROVE_SPEC.loader.exec_module(self_improve)

# The real, committed schema-conformance engine (SI-01,
# scripts/dev/check_improvement_artifacts.py) -- imported directly rather
# than hand-rolled, so PatternRecordSchemaConformanceTests below checks
# every record this module's close-out promotion/revert writes against the
# actual schemas/blocker-pattern.schema.json shape, not a re-implemented
# subset of it.
_CHECK_ARTIFACTS_SPEC = importlib.util.spec_from_file_location(
    "check_improvement_artifacts",
    REPO_ROOT / "scripts" / "dev" / "check_improvement_artifacts.py",
)
check_improvement_artifacts = importlib.util.module_from_spec(_CHECK_ARTIFACTS_SPEC)
_CHECK_ARTIFACTS_SPEC.loader.exec_module(check_improvement_artifacts)


class _RepoFixture(unittest.TestCase):
    """A real, throwaway git repository shaped the way ``plan_approval``'s
    admission gates require -- matching ``tests/test_convergence_campaign_
    driver.py``'s ``_RepoFixture`` setup."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "si05-tests@example.com")
        self._git("config", "user.name", "SI-05 Loop Tests")

        (self.repository / ".harness").mkdir()
        self._write_json(
            self.repository / ".harness" / "repository.json",
            {
                "protocol": "harness-repository-identity/1",
                "repository_id": "si05-loop-test-repository",
            },
        )
        plan_dir = self.repository / "docs" / "development"
        plan_dir.mkdir(parents=True)
        (plan_dir / "self-improvement-agent-plan.md").write_text(
            "## SI-05 — Convergence bridge, loop driver, CLI [si-05-loop]\n\n"
            "Fixture plan section for tests/test_self_improve_loop.py.\n",
            encoding="utf-8",
        )
        # The real decision template, copied verbatim: draft_decision_record
        # takes template_path as a parameter (never a hardcoded repo-absolute
        # path), but a campaign that succeeds in these tests needs a real
        # file there to read from -- exercising the shape the real
        # docs/decisions/TEMPLATE.md actually has, not a hand-simplified one.
        decisions_dir = self.repository / "docs" / "decisions"
        decisions_dir.mkdir(parents=True)
        shutil.copy(REAL_DECISION_TEMPLATE, decisions_dir / "TEMPLATE.md")
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
        """The fixture proposal with ``python3`` swapped for
        ``sys.executable`` in every assertion argv, so the assertion
        subprocess resolves on any host regardless of PATH."""

        proposal = json.loads((FIXTURE_ROOT / "proposal.json").read_text(encoding="utf-8"))
        for criterion in proposal["success_criteria"]:
            argv = criterion["assertion"]["argv"]
            if argv[0] == "python3":
                argv[0] = sys.executable
        path = self.root / "proposal.json"
        self._write_json(path, proposal)
        return path

    def _not_accepted_proposal_path(self) -> Path:
        return FIXTURE_ROOT / "proposal-not-accepted.json"

    def _proposal_path_with_ruling(self, disposition: str) -> Path:
        """The fixture proposal with its ruling disposition swapped to
        ``disposition`` -- a "not accept" ruling that is nonetheless present
        (unlike ``proposal-not-accepted.json``'s ``ruling: null``), to cover
        the reject/waive half of "refuses a proposal without an accept
        ruling"."""

        proposal = json.loads((FIXTURE_ROOT / "proposal.json").read_text(encoding="utf-8"))
        for criterion in proposal["success_criteria"]:
            argv = criterion["assertion"]["argv"]
            if argv[0] == "python3":
                argv[0] = sys.executable
        proposal["ruling"]["disposition"] = disposition
        path = self.root / f"proposal-{disposition}.json"
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
        """Real ``prepare_approval`` + hand-authored operator approval +
        real ``issue_receipt`` -- the same two-gate path
        ``scripts/approve_plan.py prepare``/``issue`` exercises, run
        directly against the library so the test controls every input
        deterministically."""

        approval_dir = round.round_dir / "approval"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=round.decomposition_path,
            output_directory=approval_dir,
        )
        # issue_receipt's acknowledgment gate only ever collects high-severity
        # warnings into "outstanding" (_require_acknowledged_high_warnings);
        # acknowledging a lower-severity one too would make it "unknown" --
        # absent from that set -- and issuance would refuse.
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
            "statement": "fixture approval for SI-05 loop-driver tests",
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


def _stub_launch_writing_fix(request: "loop.RoundLaunchRequest") -> "loop.RoundLaunchResult":
    """Stands in for a real PlanGraph attempt: no live model, no real
    browser -- it just deposits the fix a real round would have produced,
    so ``remeasure``'s re-executed assertions have something real to
    observe."""

    (request.repository / "src" / "pkg_a" / "module_a.py").write_text(
        "MARKER_A = True\n", encoding="utf-8",
    )
    (request.repository / "src" / "pkg_b" / "module_b.py").write_text(
        "MARKER_B = True\n", encoding="utf-8",
    )
    return loop.RoundLaunchResult(success=True, detail={"stub": "fix-applied"})


def _stub_launch_failing(request: "loop.RoundLaunchRequest") -> "loop.RoundLaunchResult":
    return loop.RoundLaunchResult(success=False, detail={"stub": "no-op-failure"})


def _always(result: str):
    def _runner(key, assertion, repository):
        return result
    return _runner


# ---------------------------------------------------------------------------
# AC-SI05-1: open refuses without an accept ruling; an accepted proposal
# seeds a real ConvergenceLedger whose key set equals the criteria's keys.
# ---------------------------------------------------------------------------


class OpenCampaignTests(_RepoFixture):
    def test_refuses_a_proposal_without_an_accept_ruling(self) -> None:
        with self.assertRaises(loop.ProposalNotAccepted):
            loop.open_campaign(
                repository=self.repository,
                proposal_path=self._not_accepted_proposal_path(),
            )
        campaigns_root = self.repository / "logs" / "improvement" / "campaigns"
        self.assertFalse(campaigns_root.exists())

    def test_refuses_a_proposal_with_a_reject_or_waive_disposition_ruling(self) -> None:
        for disposition in ("reject", "waive"):
            with self.subTest(disposition=disposition):
                with self.assertRaises(loop.ProposalNotAccepted):
                    loop.open_campaign(
                        repository=self.repository,
                        proposal_path=self._proposal_path_with_ruling(disposition),
                        campaign_id=f"proposal-{disposition}",
                    )

    def test_target_digest_addresses_the_committed_snapshot_content(self) -> None:
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        first_line = (opened.root / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[0]
        campaign_record = json.loads(first_line)
        snapshot_bytes = (opened.root / "proposal.json").read_bytes()
        expected_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        # A content-addressed seal: the recorded target digest addresses
        # exactly the bytes the snapshot_path names, not a digest of the
        # source computed separately from a re-serialized copy.
        self.assertEqual(campaign_record["target"]["digest"], expected_digest)
        self.assertEqual(campaign_record["seed_audit_digest"], f"seed:{expected_digest}")

    def test_accepted_proposal_opens_a_campaign_root_under_logs_improvement(self) -> None:
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        expected_root = (
            self.repository.resolve() / "logs" / "improvement" / "campaigns" / opened.campaign_id
        )
        self.assertEqual(opened.root, expected_root)
        self.assertTrue((opened.root / "ledger.jsonl").is_file())
        self.assertTrue((opened.root / "proposal.json").is_file())
        # Never under docs/improvement/ (operator ruling,
        # checker-default-root-vs-committed-decompositions).
        self.assertNotIn("docs/improvement", str(opened.root))

    def test_seed_key_set_equals_success_criteria_file_subject_pairs(self) -> None:
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        expected = frozenset({
            ("src/pkg_a/module_a.py", "marker-a-not-set"),
            ("src/pkg_b/module_b.py", "marker-b-not-set"),
        })
        self.assertEqual(frozenset(opened.seed_keys), expected)

        ledger = loop.ConvergenceLedger(opened.root / "ledger.jsonl")
        self.assertEqual(ledger.open_set(), expected)

    def test_reopening_an_already_opened_campaign_is_refused(self) -> None:
        loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        with self.assertRaises(loop.CampaignAlreadyOpen):
            loop.open_campaign(
                repository=self.repository, proposal_path=self._accepted_proposal_path(),
            )


# ---------------------------------------------------------------------------
# AC-SI05-2: round synthesizes one run per required_paths group; dispatch
# refuses without an issued receipt for exactly that decomposition.
# ---------------------------------------------------------------------------


class RoundDispatchTests(_RepoFixture):
    def _open(self) -> "loop.OpenedCampaign":
        return loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )

    def test_round_synthesizes_one_run_per_required_paths_group(self) -> None:
        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        # Two disjoint required_paths groups (pkg_a, pkg_b) -> two repair
        # runs, plus the one join-and-regression run.
        self.assertEqual(set(round.findings_by_run), {"repair-1", "repair-2"})
        run_ids = {run["id"] for run in round.decomposition["runs"]}
        self.assertEqual(run_ids, {"repair-1", "repair-2", "join-regression"})
        self.assertTrue(round.decomposition_path.is_file())
        self.assertIn(
            "logs/improvement/campaigns", round.decomposition_path.as_posix(),
        )
        self.assertNotIn("docs/improvement", round.decomposition_path.as_posix())
        # The decomposition and findings-by-run are committed --
        # prepare_approval requires the decomposition's git blob to exist
        # at base_commit. The rest of the campaign root (ledger, checkpoint,
        # proposal snapshot) stays uncommitted operational state.
        resolved_repository = self.repository.resolve()
        relative_decomposition = round.decomposition_path.relative_to(resolved_repository).as_posix()
        relative_findings = round.findings_by_run_path.relative_to(resolved_repository).as_posix()
        self.assertEqual(
            self._git("status", "--porcelain", "--", relative_decomposition, relative_findings),
            "",
        )
        # Every synthesized run carries the cited plan section's real
        # regression gates, not plan_synthesis's default referent-existence
        # check.
        for run in round.decomposition["runs"]:
            argv_text = " ".join(run["verification_argv"])
            self.assertIn("pytest", argv_text)
            self.assertIn("check_repository_contracts.py", argv_text)

    def test_ac_si05_2_round_synthesis_is_wired_through_convergence_ledger_open_findings(
        self,
    ) -> None:
        """AC-SI05-2's own wording: ``round`` synthesizes "from
        ``ConvergenceLedger.open_findings`` via ``plan_synthesis``" -- an
        explicit spy on the real bound method (never a stand-in), proving
        ``synthesize_round`` reaches ``plan_synthesis`` with a live ledger
        whose ``open_findings`` is actually invoked, not some other
        accessor that happens to produce the same key set."""

        opened = self._open()
        original = loop.ConvergenceLedger.open_findings
        with patch.object(
            loop.ConvergenceLedger, "open_findings", autospec=True, side_effect=original,
        ) as spy:
            round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self.assertTrue(spy.called)
        # The findings the spy actually returned are exactly the seed keys
        # plan_synthesis grouped into the two repair runs below.
        (called_ledger,), _kwargs = spy.call_args
        self.assertIsInstance(called_ledger, loop.ConvergenceLedger)
        self.assertEqual(set(round.findings_by_run), {"repair-1", "repair-2"})

    def test_round_commit_succeeds_when_logs_improvement_is_gitignored(self) -> None:
        """Cited plan section SI-00 gitignores ``logs/improvement/**``
        (sibling criterion AC-SI06-1) -- ``prepare_approval`` still requires
        the round's decomposition to already be a committed git blob, so
        ``synthesize_round`` must still add and commit it under that
        ignore rule, not silently skip the commit or fail on a plain
        (non-forced) ``git add``."""

        (self.repository / ".gitignore").write_text("logs/improvement/\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "gitignore logs/improvement/")

        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)

        resolved_repository = self.repository.resolve()
        relative_decomposition = round.decomposition_path.relative_to(resolved_repository).as_posix()
        relative_findings = round.findings_by_run_path.relative_to(resolved_repository).as_posix()
        tracked = self._git(
            "ls-files", "--", relative_decomposition, relative_findings,
        ).splitlines()
        self.assertEqual(sorted(tracked), sorted([relative_decomposition, relative_findings]))
        # A round with no committed decomposition would leave prepare_approval
        # with nothing to admit -- prove the downstream door still opens.
        self._approve_round(round)

    def test_round_commit_leaves_other_staged_work_untouched(self) -> None:
        opened = self._open()
        # Simulate an operator's own unrelated staged work sitting in the
        # index before this round is synthesized.
        (self.repository / "unrelated.txt").write_text("operator work in progress\n", encoding="utf-8")
        self._git("add", "unrelated.txt")

        loop.synthesize_round(repository=self.repository, campaign_root=opened.root)

        # A bare `git commit` would have swept unrelated.txt into the round's
        # decomposition commit; a pathspec-scoped commit leaves it exactly as
        # staged.
        self.assertEqual(
            self._git("status", "--porcelain", "--", "unrelated.txt"), "A  unrelated.txt",
        )
        committed_files = self._git("show", "--name-only", "--format=", "HEAD")
        self.assertNotIn("unrelated.txt", committed_files.splitlines())

    def test_default_launch_refuses_a_repository_other_than_the_harness_checkout(self) -> None:
        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)

        # The fixture repository is a throwaway tempdir, never
        # campaign_launcher.ROOT (the harness checkout) -- the production
        # launch must refuse rather than silently dispatch against the
        # wrong tree.
        with self.assertRaises(loop.ImprovementLoopError):
            loop.dispatch_round(
                repository=self.repository, campaign_root=opened.root, round=round,
            )

    def test_dispatch_refuses_without_any_issued_receipt(self) -> None:
        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        with self.assertRaises(loop.ReceiptMissing):
            loop.dispatch_round(
                repository=self.repository, campaign_root=opened.root, round=round,
                launch=_stub_launch_writing_fix,
            )

    def test_dispatch_refuses_a_receipt_issued_for_a_different_decomposition(self) -> None:
        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)

        mutated_decomposition = copy.deepcopy(round.decomposition)
        mutated_decomposition["runs"][0]["objective"] += " (mutated for the mismatch test)"
        mismatched_round = dataclasses.replace(round, decomposition=mutated_decomposition)

        with self.assertRaises(loop.ReceiptMismatch):
            loop.dispatch_round(
                repository=self.repository, campaign_root=opened.root, round=mismatched_round,
                launch=_stub_launch_writing_fix,
            )

    def test_dispatch_launches_through_an_issued_receipt_and_claims_the_fix(self) -> None:
        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)

        outcome = loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round,
            launch=_stub_launch_writing_fix,
        )
        self.assertTrue(outcome.success)

        ledger = loop.ConvergenceLedger(opened.root / "ledger.jsonl")
        for key in opened.seed_keys:
            self.assertEqual(ledger.key_status(key), "fix_claimed")

        status = loop.campaign_status(campaign_root=opened.root)
        self.assertEqual(status.rounds_completed, 1)

    def test_dispatch_accepts_a_receipt_issued_outside_the_round_directory(self) -> None:
        opened = self._open()
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)

        # Approve into a directory that is *not* round.round_dir / "approval"
        # -- the receipt's own subject reference must resolve relative to
        # wherever the receipt itself lives, never a hardcoded round-relative
        # guess, or a --receipt outside the round directory would read the
        # wrong (or no) file.
        external_dir = self.root / "external-approval"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=round.decomposition_path,
            output_directory=external_dir,
        )
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "fixture: acknowledged"}
            for warning in prepared.warnings
            if warning.get("severity") == "high"
        ]
        operator_approval_path = external_dir / "operator-approval.json"
        payload = {
            "protocol": OPERATOR_APPROVAL_PROTOCOL,
            "subject_sha256": prepared.subject_sha256,
            "actor": "operator",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "statement": "fixture approval issued outside the round directory",
        }
        if acknowledgements:
            payload["warning_acknowledgements"] = acknowledgements
        self._write_json(operator_approval_path, payload)
        receipt_path = external_dir / "receipt.json"
        issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator_approval_path,
            receipt_path=receipt_path,
        )

        outcome = loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round,
            receipt_path=receipt_path, launch=_stub_launch_writing_fix,
        )
        self.assertTrue(outcome.success)


# ---------------------------------------------------------------------------
# AC-SI05-3 / AC-SI05-4: remeasure folds verdicts; only observed_fixed
# closes a key; an unexecuted assertion blocks success; the round bound
# closes the campaign incomplete and reverts its pattern to candidate.
# Exercised as one open -> round(stub) -> remeasure -> close pipeline.
# ---------------------------------------------------------------------------


class RemeasureAndCloseTests(_RepoFixture):
    def test_observed_fixed_closes_every_key_and_succeeds_the_campaign(self) -> None:
        pattern_path = self._write_pattern("pattern-si05-fixture-01", status="proposed")
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)
        dispatch_outcome = loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round,
            launch=_stub_launch_writing_fix,
        )
        self.assertTrue(dispatch_outcome.success)

        outcome = loop.remeasure(repository=self.repository, campaign_root=opened.root)
        self.assertEqual(frozenset(outcome.observed_fixed), frozenset(opened.seed_keys))
        self.assertEqual(outcome.open_keys, ())
        self.assertEqual(outcome.lifecycle, loop.LIFECYCLE_SUCCEEDED)

        status = loop.campaign_status(campaign_root=opened.root)
        self.assertEqual(status.lifecycle, loop.LIFECYCLE_SUCCEEDED)
        self.assertEqual(status.open_keys, ())

        # Close-out promotion: a campaign that terminates successfully flips
        # every cited pattern to "addressed" (cited plan section SI-05), and
        # -- schemas/blocker-pattern.schema.json requires both of these for
        # that status -- stamps the closing campaign_id and the
        # post-integration landing_commit, matching the schema's own
        # ^[0-9a-f]{7,40}$ pattern.
        self.assertIn("pattern-si05-fixture-01", outcome.promoted_pattern_ids)
        promoted_pattern = json.loads(pattern_path.read_text(encoding="utf-8"))
        self.assertEqual(promoted_pattern["status"], "addressed")
        self.assertEqual(promoted_pattern["campaign_id"], opened.campaign_id)
        self.assertRegex(promoted_pattern["landing_commit"], _LANDING_COMMIT_RE)
        head = self._git("rev-parse", "HEAD")
        self.assertEqual(promoted_pattern["landing_commit"], head)

        # draft_decision_record is called from remeasure()'s success branch
        # and its path is recorded on the outcome and in the checkpoint
        # state -- never written directly into the real docs/decisions/.
        self.assertIsNotNone(outcome.decision_draft_path)
        draft_path = Path(outcome.decision_draft_path)
        self.assertTrue(draft_path.is_file())
        self.assertTrue(str(draft_path).startswith(str(opened.root)))
        real_decisions_dir = self.repository / "docs" / "decisions"
        for entry in real_decisions_dir.iterdir():
            self.assertEqual(entry.name, "TEMPLATE.md")

        checkpoint_state = json.loads(
            (opened.root / "checkpoint.json").read_text(encoding="utf-8")
        )["state"]
        self.assertEqual(checkpoint_state["decision_draft_path"], outcome.decision_draft_path)

        draft_text = draft_path.read_text(encoding="utf-8")
        self.assertIn("Concerns-paths: src/pkg_a/module_a.py, src/pkg_b/module_b.py", draft_text)
        self.assertIn(f"Valid-from-commit: {head}", draft_text)
        self.assertIn(f"Run: campaign {opened.campaign_id}", draft_text)
        self.assertIn("## Validation and reversal", draft_text)
        self.assertIn("observed_fixed or excluded", draft_text)
        self.assertIn(head, draft_text)

    def test_still_broken_and_unexecuted_are_reported_separately(self) -> None:
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        keys = sorted(opened.seed_keys)
        one_broken, other_unexecuted = keys[0], keys[1]

        def _runner(key, assertion, repository):
            return "fail" if key == one_broken else "unexecuted"

        outcome = loop.remeasure(
            repository=self.repository, campaign_root=opened.root, assertion_runner=_runner,
        )
        # A "fail" against a key never claimed fixed is observed broken --
        # distinct from an assertion that never ran at all -- but neither
        # closes the key nor blocks on its own; termination is unaffected.
        self.assertEqual(outcome.still_broken, (one_broken,))
        self.assertEqual(outcome.unexecuted, (other_unexecuted,))
        self.assertEqual(outcome.reopened, ())
        self.assertEqual(outcome.lifecycle, loop.LIFECYCLE_OPEN)
        self.assertEqual(frozenset(outcome.open_keys), frozenset(keys))
        self.assertIsNone(outcome.decision_draft_path)

    def test_a_second_remeasure_with_changed_results_is_not_swallowed_by_idempotence(
        self,
    ) -> None:
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        # Both remeasures below run at round 0 (no dispatch in between), so
        # the open-key set never changes between them; only the *results*
        # of the assertion re-check do.
        first = loop.remeasure(
            repository=self.repository, campaign_root=opened.root,
            assertion_runner=_always("unexecuted"),
        )
        self.assertEqual(first.observed_fixed, ())
        self.assertEqual(frozenset(first.open_keys), frozenset(opened.seed_keys))

        second = loop.remeasure(
            repository=self.repository, campaign_root=opened.root,
            assertion_runner=_always("pass"),
        )
        # A digest built only from (campaign_id, round, open-key-set) would
        # be identical to the first remeasure's, and
        # ConvergenceLedger.ingest_audit's digest-idempotence would silently
        # swallow this second, genuinely different result.
        self.assertNotEqual(second.digest, first.digest)
        self.assertEqual(frozenset(second.observed_fixed), frozenset(opened.seed_keys))
        self.assertEqual(second.open_keys, ())
        self.assertEqual(second.lifecycle, loop.LIFECYCLE_SUCCEEDED)

    def test_unexecuted_assertion_blocks_success_and_round_bound_reverts_pattern(self) -> None:
        pattern_path = self._write_pattern("pattern-si05-fixture-01", status="proposed")
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
            round_bound=2,
        )

        # Round 1: a real receipt-gated dispatch that never actually lands
        # the fix (the stub always fails), so every assertion re-check
        # below finds nothing to observe.
        round_1 = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round_1)
        loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round_1,
            launch=_stub_launch_failing,
        )
        first_remeasure = loop.remeasure(
            repository=self.repository, campaign_root=opened.root,
            assertion_runner=_always("unexecuted"),
        )
        self.assertEqual(first_remeasure.lifecycle, loop.LIFECYCLE_OPEN)
        self.assertEqual(
            frozenset(first_remeasure.unexecuted), frozenset(opened.seed_keys),
        )
        self.assertEqual(
            frozenset(first_remeasure.open_keys), frozenset(opened.seed_keys),
        )

        # Round 2 hits the configured round_bound=2 with keys still open.
        round_2 = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round_2)
        loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round_2,
            launch=_stub_launch_failing,
        )
        second_remeasure = loop.remeasure(
            repository=self.repository, campaign_root=opened.root,
            assertion_runner=_always("unexecuted"),
        )
        self.assertEqual(second_remeasure.lifecycle, loop.LIFECYCLE_INCOMPLETE)
        self.assertIn("pattern-si05-fixture-01", second_remeasure.reverted_pattern_ids)
        self.assertIsNone(second_remeasure.decision_draft_path)

        status = loop.campaign_status(campaign_root=opened.root)
        self.assertEqual(status.lifecycle, loop.LIFECYCLE_INCOMPLETE)
        self.assertEqual(status.rounds_completed, 2)

        reverted_pattern = json.loads(pattern_path.read_text(encoding="utf-8"))
        self.assertEqual(reverted_pattern["status"], "candidate")
        self.assertNotIn("campaign_id", reverted_pattern)
        self.assertNotIn("landing_commit", reverted_pattern)


# ---------------------------------------------------------------------------
# AC-SI05-3 (schema conformance): the pattern record close-out promotion
# (status -> "addressed", campaign_id + landing_commit stamped) and the
# round-bound revert (status -> "candidate") both write records that
# validate cleanly against the real schemas/blocker-pattern.schema.json --
# through the same hand-written engine scripts/dev/check_improvement_
# artifacts.py's committed-artifact gate runs, not a regex/presence check
# on two fields alone.
# ---------------------------------------------------------------------------


#: A pattern record shaped to satisfy every schemas/blocker-pattern.schema.json
#: property blocker-pattern.schema.json's own validate_artifact enforces --
#: unlike RunAuditTests' minimal _proposable_pattern() defaults (a bare
#: "fixture" classification and a null generalizability.verdict, neither of
#: which is schema-valid), this overrides every property the schema
#: constrains beyond a bare type check: a real classification enum member,
#: a fully-populated cost_aggregate (each cost_stat requires its own
#: median/tail), and a generalizability verdict already filled -- exactly
#: the state SI-04 leaves a pattern in before drafting a proposal from it
#: (plan section SI-04: "the generalizability verdict field ... is filled
#: by the bounded model step in SI-04"), which is the only state a pattern
#: SI-05 opens a campaign against is ever actually in.
_SCHEMA_VALID_PATTERN_OVERRIDES: dict[str, object] = {
    "classification": "product",
    "cost_aggregate": {
        "wall_clock_ms": {"median": 0, "tail": 0},
        "tokens": {"median": 0, "tail": 0},
        "diff_churn_lines": {"median": 0, "tail": 0},
    },
    "generalizability": {
        "verdict": "policy_gap",
        "rubric_id": "burden-admission/1",
        "rationale": (
            "fixture: SI-04 already ruled on this pattern before SI-05 "
            "opened a campaign against it."
        ),
        "counterexamples": [],
    },
}


class PatternRecordSchemaConformanceTests(_RepoFixture):
    def _write_schema_valid_pattern(self, pattern_id: str, *, status: str) -> Path:
        pattern = _proposable_pattern(
            pattern_id=pattern_id, status=status, **_SCHEMA_VALID_PATTERN_OVERRIDES,
        )
        path = self.repository / "logs" / "improvement" / "patterns" / f"{pattern_id}.json"
        self._write_json(path, pattern)
        return path

    def test_close_out_promoted_pattern_record_validates_against_the_real_schema(
        self,
    ) -> None:
        pattern_path = self._write_schema_valid_pattern(
            "pattern-si05-fixture-01", status="proposed",
        )
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)
        loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round,
            launch=_stub_launch_writing_fix,
        )
        outcome = loop.remeasure(repository=self.repository, campaign_root=opened.root)
        self.assertEqual(outcome.lifecycle, loop.LIFECYCLE_SUCCEEDED)

        promoted = json.loads(pattern_path.read_text(encoding="utf-8"))
        self.assertEqual(promoted["status"], "addressed")
        errors = check_improvement_artifacts.validate_artifact(promoted, str(pattern_path))
        self.assertEqual(
            errors, [],
            f"close-out-promoted pattern record fails blocker-pattern.schema.json: {errors}",
        )

    def test_round_bound_reverted_pattern_record_validates_against_the_real_schema(
        self,
    ) -> None:
        pattern_path = self._write_schema_valid_pattern(
            "pattern-si05-fixture-01", status="proposed",
        )
        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
            round_bound=1,
        )
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)
        loop.dispatch_round(
            repository=self.repository, campaign_root=opened.root, round=round,
            launch=_stub_launch_failing,
        )
        outcome = loop.remeasure(
            repository=self.repository, campaign_root=opened.root,
            assertion_runner=_always("unexecuted"),
        )
        self.assertEqual(outcome.lifecycle, loop.LIFECYCLE_INCOMPLETE)

        reverted = json.loads(pattern_path.read_text(encoding="utf-8"))
        self.assertEqual(reverted["status"], "candidate")
        errors = check_improvement_artifacts.validate_artifact(reverted, str(pattern_path))
        self.assertEqual(
            errors, [],
            f"round-bound-reverted pattern record fails blocker-pattern.schema.json: {errors}",
        )


# ---------------------------------------------------------------------------
# AC-SI05-4: the fixture campaign's full open -> round(stub) -> remeasure ->
# close pipeline never touches the production PlanGraph launch path and
# never imports a real-browser dependency -- the "no live model, no real
# browser" clause made an explicit, checkable assertion rather than an
# implication of "the stub was passed".
# ---------------------------------------------------------------------------


class NoLiveModelNoRealBrowserTests(_RepoFixture):
    def test_ac_si05_4_full_lifecycle_never_calls_the_production_launch_or_imports_a_browser(
        self,
    ) -> None:
        self._write_pattern("pattern-si05-fixture-01", status="proposed")
        for module_name in ("playwright", "selenium"):
            self.assertNotIn(
                module_name, sys.modules,
                f"{module_name} must not already be imported before this test runs",
            )

        opened = loop.open_campaign(
            repository=self.repository, proposal_path=self._accepted_proposal_path(),
        )
        round = loop.synthesize_round(repository=self.repository, campaign_root=opened.root)
        self._approve_round(round)

        with patch.object(
            loop, "_default_launch",
            side_effect=AssertionError(
                "the production campaign_launcher-backed launch must never be "
                "invoked by a fixture campaign driven with an injected stub"
            ),
        ):
            dispatch_outcome = loop.dispatch_round(
                repository=self.repository, campaign_root=opened.root, round=round,
                launch=_stub_launch_writing_fix,
            )
        self.assertTrue(dispatch_outcome.success)

        outcome = loop.remeasure(repository=self.repository, campaign_root=opened.root)
        self.assertEqual(outcome.lifecycle, loop.LIFECYCLE_SUCCEEDED)

        status = loop.campaign_status(campaign_root=opened.root)
        self.assertEqual(status.lifecycle, loop.LIFECYCLE_SUCCEEDED)
        self.assertEqual(status.open_keys, ())

        for module_name in ("playwright", "selenium"):
            self.assertNotIn(
                module_name, sys.modules,
                f"{module_name} must not have been imported anywhere in this "
                "no-live-model, no-real-browser pipeline",
            )


# ---------------------------------------------------------------------------
# draft_decision_record: unit tests against a tmp decisions_root, independent
# of remeasure()'s wiring.
# ---------------------------------------------------------------------------


class DraftDecisionRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.campaign_root = self.root / "campaign"
        self.campaign_root.mkdir()
        self.decisions_root = self.root / "docs-decisions"
        self.decisions_root.mkdir()

    def _proposal(self) -> dict:
        return json.loads((FIXTURE_ROOT / "proposal.json").read_text(encoding="utf-8"))

    def test_number_is_one_past_the_highest_existing_decision(self) -> None:
        (self.decisions_root / "0001-something.md").write_text("x", encoding="utf-8")
        (self.decisions_root / "0007-something-else.md").write_text("x", encoding="utf-8")
        (self.decisions_root / "TEMPLATE.md").write_text("not-numbered", encoding="utf-8")

        draft_path = loop.draft_decision_record(
            campaign_root=self.campaign_root,
            proposal=self._proposal(),
            template_path=REAL_DECISION_TEMPLATE,
            decisions_root=self.decisions_root,
            campaign_id="campaign-numbering-test",
            landing_commit="a" * 40,
        )
        self.assertTrue(draft_path.name.startswith("0008-"))

    def test_number_starts_at_0001_for_an_empty_decisions_root(self) -> None:
        draft_path = loop.draft_decision_record(
            campaign_root=self.campaign_root,
            proposal=self._proposal(),
            template_path=REAL_DECISION_TEMPLATE,
            decisions_root=self.decisions_root,
            campaign_id="campaign-empty-numbering-test",
            landing_commit="b" * 40,
        )
        self.assertTrue(draft_path.name.startswith("0001-"))

    def test_draft_is_written_under_campaign_root_never_into_decisions_root(self) -> None:
        draft_path = loop.draft_decision_record(
            campaign_root=self.campaign_root,
            proposal=self._proposal(),
            template_path=REAL_DECISION_TEMPLATE,
            decisions_root=self.decisions_root,
            campaign_id="campaign-write-location-test",
            landing_commit="c" * 40,
        )
        self.assertEqual(draft_path.parent.parent, self.campaign_root)
        self.assertEqual(list(self.decisions_root.iterdir()), [])

    def test_concerns_paths_and_evidence_are_rendered(self) -> None:
        draft_path = loop.draft_decision_record(
            campaign_root=self.campaign_root,
            proposal=self._proposal(),
            template_path=REAL_DECISION_TEMPLATE,
            decisions_root=self.decisions_root,
            campaign_id="campaign-render-test",
            landing_commit="d" * 40,
            observed_fixed_keys=(("src/pkg_a/module_a.py", "marker-a-not-set"),),
            excluded_keys=(("src/pkg_b/module_b.py", "marker-b-not-set"),),
            remeasure_digest="remeasure:fixture-digest",
        )
        text = draft_path.read_text(encoding="utf-8")
        self.assertIn(
            "Concerns-paths: src/pkg_a/module_a.py, src/pkg_b/module_b.py", text,
        )
        self.assertIn(f"Valid-from-commit: {'d' * 40}", text)
        self.assertIn("Run: campaign campaign-render-test", text)
        self.assertIn("## Validation and reversal", text)
        self.assertIn("src/pkg_a/module_a.py::marker-a-not-set", text)
        self.assertIn("src/pkg_b/module_b.py::marker-b-not-set", text)
        self.assertIn("remeasure:fixture-digest", text)

    def test_refuses_when_the_template_is_missing(self) -> None:
        with self.assertRaises(loop.ImprovementLoopError):
            loop.draft_decision_record(
                campaign_root=self.campaign_root,
                proposal=self._proposal(),
                template_path=self.root / "no-such-template.md",
                decisions_root=self.decisions_root,
                campaign_id="campaign-missing-template-test",
                landing_commit="e" * 40,
            )


# ---------------------------------------------------------------------------
# audit: SI-02 mining + SI-03 clustering; --propose-if-ready gates on a real
# DecisionRegistry and the SI-03 anti-thrash ledger, and never clobbers a
# pattern record a later layer already wrote.
# ---------------------------------------------------------------------------


def _proposable_pattern(**overrides) -> dict:
    pattern = {
        "protocol": "blocker-pattern/1",
        "pattern_id": "pattern-si05-audit-fixture",
        "signature": "fixture-signature",
        "classification": "fixture",
        "status": "candidate",
        "support": {
            "observation_count": 3, "distinct_run_count": 2,
            "distinct_lineage_count": 2, "distinct_task_suite_count": 2,
        },
        "first_seen_at": "2026-08-01T00:00:00Z",
        "last_seen_at": "2026-08-01T00:00:00Z",
        "observations": [],
        "cost_aggregate": {},
        "fixes_employed": [],
        "generalizability": {
            "verdict": None, "rubric_id": "burden-admission/1",
            "rationale": "", "counterexamples": [],
        },
        "recurrence": [],
    }
    pattern.update(overrides)
    return pattern


def _audit_judgment(*, cite_decision: bool):
    """A deterministic, injectable ``JudgmentCallable`` (no live model),
    targeting ``src/pkg_a/module_a.py`` -- present in every ``_RepoFixture``
    repository."""

    def _draft(pattern: object) -> ProposalDraft:
        rationale = "fixture rationale"
        if cite_decision:
            rationale += " governed by 0001-fixture-governs-pkg-a"
        return ProposalDraft(
            question="fixture question", choice="fixture choice",
            alternatives=("fixture alternative",), rationale=rationale,
            evidence=("fixture evidence",), consequences=("fixture consequence",),
            reversible=True,
            demonstrated_failure="fixture demonstrated failure",
            production_consumer="fixture production consumer",
            end_to_end_assertion="fixture end-to-end assertion",
            target_surface=(TargetSurfaceDraft(path="src/pkg_a/module_a.py", kind="code"),),
            accuracy_risk="none",
            success_criteria=(
                SuccessCriterionDraft(
                    file="src/pkg_a/module_a.py", subject="fixture-subject",
                    required_paths=("src/pkg_a/module_a.py",),
                    statement="fixture statement",
                    assertion={"argv": ["python3", "-c", "pass"], "timeout_seconds": 30},
                ),
            ),
            rollback="fixture rollback",
        )

    return _draft


def _cli_judgment_factory():
    """Module-level, dotted-path-importable factory: ``self_improve.py
    audit --judgment tests.test_self_improve_loop:_cli_judgment_factory``
    resolves this name and calls it with no arguments to produce the
    ``JudgmentCallable`` ``run_audit`` needs."""

    return _audit_judgment(cite_decision=False)


class RunAuditTests(_RepoFixture):
    def _seed_runs(self) -> None:
        runs_root = self.repository / "logs" / "runs"
        runs_root.mkdir(parents=True)
        for name in ("run-si02-valid-001", "run-si02-valid-002"):
            shutil.copytree(CORPUS_ROOT / name, runs_root / name)

    def test_mines_and_clusters_without_crashing_on_the_now_argument(self) -> None:
        # improvement_index.cluster_observations requires the keyword-only
        # `now` argument; run_audit previously omitted it, so any repository
        # with a logs/runs/ tree raised TypeError before writing a pattern.
        self._seed_runs()
        result = loop.run_audit(repository=self.repository)
        self.assertTrue(result.observations)
        self.assertTrue(result.patterns)
        patterns_root = self.repository / "logs" / "improvement" / "patterns"
        for pattern in result.patterns:
            self.assertTrue((patterns_root / f"{pattern['pattern_id']}.json").is_file())

    def test_re_audit_preserves_a_later_layers_status_and_verdict(self) -> None:
        self._seed_runs()
        first = loop.run_audit(repository=self.repository)
        pattern_id = first.patterns[0]["pattern_id"]
        pattern_path = (
            self.repository / "logs" / "improvement" / "patterns" / f"{pattern_id}.json"
        )
        pattern = json.loads(pattern_path.read_text(encoding="utf-8"))
        pattern["status"] = "addressed"
        pattern["campaign_id"] = "campaign-preserved-by-re-audit"
        pattern["landing_commit"] = "f" * 40
        pattern["generalizability"] = {
            "verdict": "policy_gap", "rubric_id": "burden-admission/1",
            "rationale": "fixture: SI-04 already ruled on this", "counterexamples": [],
        }
        self._write_json(pattern_path, pattern)

        loop.run_audit(repository=self.repository)

        reloaded = json.loads(pattern_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["status"], "addressed")
        self.assertEqual(reloaded["campaign_id"], "campaign-preserved-by-re-audit")
        self.assertEqual(reloaded["landing_commit"], "f" * 40)
        self.assertEqual(reloaded["generalizability"]["verdict"], "policy_gap")
        # Clustering-owned fields (support/observations) still refresh.
        self.assertIn("support", reloaded)

    def test_propose_path_gates_on_a_real_anti_thrash_ledger(self) -> None:
        self._seed_runs()
        pattern = _proposable_pattern()
        with patch(
            "harness_labs.observability.improvement_index.cluster_observations",
            return_value=[pattern],
        ):
            first = loop.run_audit(
                repository=self.repository, propose_if_ready=True,
                judgment=_audit_judgment(cite_decision=False),
            )
            self.assertEqual(len(first.proposals), 1)

            ledger_path = self.repository / "logs" / "improvement" / "proposal-ledger.json"
            entries = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "open")
            self.assertEqual(entries[0]["target_surface"], "src/pkg_a/module_a.py")

            # A second propose pass for the same still-open target surface is
            # refused by the anti-thrash ledger's per-surface-uniqueness
            # rule, not silently re-opened -- SI-03's re-proposal bar,
            # vacuous when the ledger is never consulted.
            second = loop.run_audit(
                repository=self.repository, propose_if_ready=True,
                judgment=_audit_judgment(cite_decision=False),
            )
            self.assertEqual(second.proposals, ())
            entries_after = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(entries_after), 1)

    def test_skipped_and_refused_reach_the_audit_result_not_just_mine(self) -> None:
        """run_forensics.mine() computes skipped/refused so a thin harvest
        always explains itself (self-improvement-agent-guide.md SS5); this
        asserts run_audit() actually threads that data through to
        AuditResult/its as_dict() instead of discarding it after mining."""
        self._seed_runs()
        runs_root = self.repository / "logs" / "runs"
        shutil.copytree(CORPUS_ROOT / "run-si02-tampered-001", runs_root / "run-si02-tampered-001")
        (runs_root / "not-a-run").mkdir()

        result = loop.run_audit(repository=self.repository)

        skipped_paths = [entry.path for entry in result.skipped]
        self.assertIn("not-a-run", skipped_paths)
        refused_dirs = [refusal.run_dir for refusal in result.refused]
        self.assertIn("run-si02-tampered-001", refused_dirs)

        as_dict = result.as_dict()
        self.assertIn("not-a-run", [entry["path"] for entry in as_dict["skipped"]])
        self.assertIn(
            "run-si02-tampered-001", [entry["run_dir"] for entry in as_dict["refused"]]
        )
        self.assertIsInstance(as_dict["excluded_run_ids"], list)

        # Strictly additive: the pre-existing three keys are unchanged.
        self.assertEqual(as_dict["observation_count"], len(result.observations))
        self.assertEqual(
            as_dict["pattern_ids"],
            sorted(str(p.get("pattern_id")) for p in result.patterns),
        )
        self.assertEqual(as_dict["proposal_ids"], [])

    def test_propose_path_refuses_an_uncited_governed_path(self) -> None:
        self._seed_runs()
        decisions_dir = self.repository / "docs" / "decisions"
        (decisions_dir / "0001-fixture-governs-pkg-a.md").write_text(
            "# 0001 — Fixture governs pkg_a\n\n"
            "Status: accepted\n"
            "Concerns-paths: src/pkg_a/module_a.py\n",
            encoding="utf-8",
        )
        pattern = _proposable_pattern()
        with patch(
            "harness_labs.observability.improvement_index.cluster_observations",
            return_value=[pattern],
        ):
            result = loop.run_audit(
                repository=self.repository, propose_if_ready=True,
                judgment=_audit_judgment(cite_decision=False),
            )
        # DecisionRegistry is real here (loaded from docs/decisions), not the
        # always-empty registry that would make this refusal vacuous.
        self.assertEqual(result.proposals, ())


# ---------------------------------------------------------------------------
# CLI: scripts/self_improve.py's open/round subcommands over the library
# (AC-SI05-1's "self_improve.py open ..." / AC-SI05-2's "self_improve.py
# round ..." clauses, otherwise unproven by any test invoking the CLI).
# ---------------------------------------------------------------------------


class SelfImproveCliTests(_RepoFixture):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self_improve.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_cli_open_refuses_a_proposal_without_an_accept_ruling(self) -> None:
        status, _out, err = self._run_cli([
            "open", "--proposal", str(self._not_accepted_proposal_path()),
            "--repository", str(self.repository),
        ])
        self.assertEqual(status, 1)
        self.assertIn("accept", err)
        self.assertFalse((self.repository / "logs" / "improvement" / "campaigns").exists())

    def test_cli_open_and_round_synthesize_against_the_library(self) -> None:
        status, out, _err = self._run_cli([
            "open", "--proposal", str(self._accepted_proposal_path()),
            "--repository", str(self.repository),
        ])
        self.assertEqual(status, 0)
        opened_payload = json.loads(out)
        campaign_id = opened_payload["campaign_id"]
        self.assertIn("logs/improvement/campaigns", opened_payload["root"])

        status, out, err = self._run_cli([
            "round", "--campaign-id", campaign_id, "--repository", str(self.repository),
        ])
        self.assertEqual(status, 0)
        round_payload = json.loads(out)
        self.assertEqual(set(round_payload["run_ids"]), {"repair-1", "repair-2"})
        self.assertIn("HALTED for operator approval", err)

        # round --launch refuses to dispatch without an issued receipt --
        # the CLI surface for AC-SI05-2's dispatch-refusal clause.
        status, _out, err = self._run_cli([
            "round", "--campaign-id", campaign_id, "--repository", str(self.repository),
            "--launch",
        ])
        self.assertEqual(status, 1)
        self.assertIn("no issued plan-approval receipt", err)

    def test_cli_audit_propose_if_ready_drafts_only_with_a_judgment_hook(self) -> None:
        runs_root = self.repository / "logs" / "runs"
        runs_root.mkdir(parents=True)
        for name in ("run-si02-valid-001", "run-si02-valid-002"):
            shutil.copytree(CORPUS_ROOT / name, runs_root / name)
        pattern = _proposable_pattern()

        # --propose-if-ready with no --judgment: mining/clustering still
        # run in full, but no proposal is fabricated from a model the CLI
        # was never given a way to name.
        with patch(
            "harness_labs.observability.improvement_index.cluster_observations",
            return_value=[pattern],
        ):
            status, out, _err = self._run_cli([
                "audit", "--repository", str(self.repository), "--propose-if-ready",
            ])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["proposal_ids"], [])

        # --propose-if-ready --judgment module:factory resolves the named
        # zero-argument factory and drafts through it.
        with patch(
            "harness_labs.observability.improvement_index.cluster_observations",
            return_value=[pattern],
        ):
            status, out, _err = self._run_cli([
                "audit", "--repository", str(self.repository), "--propose-if-ready",
                "--judgment", "tests.test_self_improve_loop:_cli_judgment_factory",
            ])
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(out)["proposal_ids"], [f"proposal-{pattern['pattern_id']}"],
        )

    def test_cli_remeasure_and_status_report_the_closed_campaign(self) -> None:
        status, out, _err = self._run_cli([
            "open", "--proposal", str(self._accepted_proposal_path()),
            "--repository", str(self.repository),
        ])
        self.assertEqual(status, 0)
        campaign_id = json.loads(out)["campaign_id"]
        campaign_root = loop.campaign_root_for(self.repository, campaign_id)

        round = loop.synthesize_round(repository=self.repository, campaign_root=campaign_root)
        self._approve_round(round)
        loop.dispatch_round(
            repository=self.repository, campaign_root=campaign_root, round=round,
            launch=_stub_launch_writing_fix,
        )

        status, out, _err = self._run_cli([
            "remeasure", "--campaign-id", campaign_id, "--repository", str(self.repository),
        ])
        self.assertEqual(status, 0)
        remeasure_payload = json.loads(out)
        self.assertEqual(remeasure_payload["lifecycle"], loop.LIFECYCLE_SUCCEEDED)
        self.assertIsNotNone(remeasure_payload["decision_draft_path"])

        status, out, _err = self._run_cli([
            "status", "--campaign-id", campaign_id, "--repository", str(self.repository),
        ])
        self.assertEqual(status, 0)
        status_payload = json.loads(out)
        self.assertEqual(status_payload["lifecycle"], loop.LIFECYCLE_SUCCEEDED)
        self.assertEqual(status_payload["open_keys"], [])


if __name__ == "__main__":
    unittest.main()
