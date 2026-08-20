"""Tests for finding intake (DTR-FI: AC-FI-1..AC-FI-5).

Round-trips every drafted finding through the REAL
``ConvergenceLedger.ingest_audit`` on a scratch ledger -- never a
reimplementation of the validator's rules -- and drives ``scripts/report_finding.py``
in-process (via ``importlib``) so ``ConvergenceLedger.ingest_audit`` can be
patched to prove the CLI never calls it.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.plangraph.convergence_campaign import CampaignArtifactStore
from harness_labs.plangraph.convergence_ledger import ConvergenceLedger, _validate_finding
from harness_labs.plangraph.finding_intake import (
    DraftFinding,
    FindingIntakeError,
    IntakeQuestion,
    draft_finding,
    draft_findings_batch,
    seal_findings,
)

REPO = Path(__file__).resolve().parents[1]
_SCRIPT = REPO / "scripts" / "report_finding.py"
_SPEC = importlib.util.spec_from_file_location("report_finding", _SCRIPT)
report_finding = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_finding)

STATEMENT_COMPUTE_TOTAL = (
    "The `compute_total` helper in pkg_a mis-sums empty batches."
)
STATEMENT_RENDER_WIDGET = (
    "The `render_widget` function loses focus state on re-render."
)
STATEMENT_PATH_CITED = (
    "The behavior described in `pkg_b/module_two.py` drops widget "
    "context on retry."
)
STATEMENT_DISJOINT = (
    "The `duplicate_symbol` helper returns the wrong value."
)
STATEMENT_NO_CANDIDATE = (
    "The `totally_missing_symbol_zzz` crashes on startup."
)
STATEMENT_NO_TERM = (
    "This whole workflow feels clunky and should be nicer."
)
STATEMENT_MULTI_TERM_DISJOINT = (
    "The `compute_total` result is inconsistent with what `render_widget` "
    "displays."
)
STATEMENT_COMPUTE_TOTAL_ALT = (
    "The `compute_total` helper also mishandles negative amounts."
)


def _make_repo_tree(root: Path) -> Path:
    (root / "pkg_a").mkdir(parents=True)
    (root / "pkg_a" / "module_one.py").write_text(
        "def compute_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (root / "pkg_b").mkdir(parents=True)
    (root / "pkg_b" / "module_two.py").write_text(
        "def render_widget(ctx):\n    return ctx\n", encoding="utf-8"
    )
    (root / "dup").mkdir(parents=True)
    (root / "dup" / "left.py").write_text(
        "def duplicate_symbol():\n    return 1\n", encoding="utf-8"
    )
    (root / "dup" / "right.py").write_text(
        "def duplicate_symbol():\n    return 2\n", encoding="utf-8"
    )
    return root


class FindingIntakeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo_root = _make_repo_tree(self.root / "repo")
        self.ledger_path = self.root / "campaign" / "ledger.jsonl"
        self.campaign_root = self.root / "campaign"
        self.store = CampaignArtifactStore(self.campaign_root / "artifacts")


# -- AC-FI-2: ambiguity is returned as a question, never guessed ------------


class AmbiguityTests(FindingIntakeTestCase):
    def test_disjoint_candidates_yield_intake_question(self) -> None:
        result = draft_finding(
            STATEMENT_DISJOINT, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(result, IntakeQuestion)
        self.assertEqual(
            {candidate[0] for candidate in result.candidates},
            {"dup/left.py", "dup/right.py"},
        )

    def test_no_candidate_yields_intake_question(self) -> None:
        result = draft_finding(
            STATEMENT_NO_CANDIDATE, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(result, IntakeQuestion)
        self.assertEqual(result.candidates, ())

    def test_multiple_disjoint_terms_yield_intake_question(self) -> None:
        # Two backtick-quoted terms that each resolve to exactly one file,
        # but to two different files, must not be attributed to whichever
        # term happens to appear first in the statement.
        result = draft_finding(
            STATEMENT_MULTI_TERM_DISJOINT, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(result, IntakeQuestion)
        self.assertEqual(
            {candidate[0] for candidate in result.candidates},
            {"pkg_a/module_one.py", "pkg_b/module_two.py"},
        )

    def test_no_searchable_terms_yields_intake_question(self) -> None:
        result = draft_finding(
            STATEMENT_NO_TERM, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(result, IntakeQuestion)
        self.assertIn("backtick", result.reason)

    def test_ambiguous_statement_seals_nothing(self) -> None:
        argv = [
            STATEMENT_DISJOINT,
            "--ledger", str(self.ledger_path),
            "--campaign-root", str(self.campaign_root),
            "--repo-root", str(self.repo_root),
            "--target", "t",
        ]
        exit_code = report_finding.main(argv)
        self.assertEqual(exit_code, 1)
        self.assertFalse((self.campaign_root / "artifacts" / "objects").exists())


# -- AC-FI-1: drafted findings round-trip the real ledger validator ---------


class DraftFindingRealLedgerTests(FindingIntakeTestCase):
    def test_single_finding_round_trips_real_ledger(self) -> None:
        finding = draft_finding(
            STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(finding, DraftFinding)
        self.assertEqual(finding.file, "pkg_a/module_one.py")
        self.assertEqual(finding.required_paths, ("pkg_a/module_one.py",))

        ledger = ConvergenceLedger(self.ledger_path)
        audit = {
            "digest": "d1", "findings": [finding.to_envelope()],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        }
        summary = ledger.ingest_audit(audit)  # must not raise
        self.assertEqual(summary["opened"], [[finding.file, finding.subject]])
        self.assertEqual(
            ledger.open_set(), frozenset({(finding.file, finding.subject)})
        )

    def test_envelope_carries_all_twelve_validator_fields(self) -> None:
        finding = draft_finding(
            STATEMENT_PATH_CITED, repo_root=self.repo_root, target="t",
            evidence_refs=("cap-1",),
        )
        self.assertIsInstance(finding, DraftFinding)
        envelope = finding.to_envelope()
        # Derive the expected field set from the real validator itself
        # rather than a hand-copied mirror of its rules, so a validator
        # field being added or renamed would fail this assertion.
        self.assertEqual(set(envelope), set(_validate_finding(envelope)))
        self.assertEqual(len(envelope), 12)
        self.assertEqual(envelope["confidence"], "C+S")
        self.assertFalse(envelope["requires_disposition"])
        self.assertEqual(envelope["evidence_refs"], ["cap-1"])

        ledger = ConvergenceLedger(self.ledger_path)
        ledger.ingest_audit({
            "digest": "d1", "findings": [envelope],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        })

    def test_multiple_findings_open_set_matches_exactly(self) -> None:
        first = draft_finding(
            STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t"
        )
        second = draft_finding(
            STATEMENT_RENDER_WIDGET, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(first, DraftFinding)
        self.assertIsInstance(second, DraftFinding)

        ledger = ConvergenceLedger(self.ledger_path)
        ledger.ingest_audit({
            "digest": "d1",
            "findings": [first.to_envelope(), second.to_envelope()],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        })
        self.assertEqual(
            ledger.open_set(),
            frozenset({
                (first.file, first.subject), (second.file, second.subject),
            }),
        )


# -- draft_finding's public keyword surface is validated, not just its --
# -- CLI-supplied defaults; requires_disposition is independent of evidence --


class KeywordSurfaceTests(FindingIntakeTestCase):
    def test_invalid_severity_raises_finding_intake_error(self) -> None:
        with self.assertRaisesRegex(FindingIntakeError, "severity"):
            draft_finding(
                STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t",
                severity="blocker",
            )

    def test_empty_evidence_ref_raises_finding_intake_error(self) -> None:
        with self.assertRaisesRegex(FindingIntakeError, "evidence_refs"):
            draft_finding(
                STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t",
                evidence_refs=("",),
            )

    def test_empty_source_finding_id_raises_finding_intake_error(self) -> None:
        with self.assertRaisesRegex(FindingIntakeError, "source_finding_ids"):
            draft_finding(
                STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t",
                source_finding_ids=("",),
            )

    def test_bad_supersedes_key_raises_finding_intake_error(self) -> None:
        with self.assertRaisesRegex(FindingIntakeError, "supersedes_key"):
            draft_finding(
                STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t",
                supersedes_key=("only-one-part",),
            )

    def test_valid_supersedes_key_round_trips_real_ledger(self) -> None:
        finding = draft_finding(
            STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t",
            supersedes_key=("pkg_a/module_one.py", "compute_total"),
        )
        self.assertIsInstance(finding, DraftFinding)
        self.assertEqual(
            finding.supersedes_key, ("pkg_a/module_one.py", "compute_total")
        )
        envelope = finding.to_envelope()
        self.assertEqual(
            envelope["supersedes_key"], ["pkg_a/module_one.py", "compute_total"]
        )

        ledger = ConvergenceLedger(self.ledger_path)
        ledger.ingest_audit({
            "digest": "d1", "findings": [envelope],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        })

    def test_requires_disposition_independent_of_evidence(self) -> None:
        judgment_call_with_evidence = draft_finding(
            STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t",
            evidence_refs=("cap-1",), requires_disposition=True,
        )
        self.assertIsInstance(judgment_call_with_evidence, DraftFinding)
        self.assertEqual(judgment_call_with_evidence.confidence, "C+S")
        self.assertTrue(judgment_call_with_evidence.requires_disposition)

        observed_fact_without_evidence = draft_finding(
            STATEMENT_RENDER_WIDGET, repo_root=self.repo_root, target="t",
            requires_disposition=False,
        )
        self.assertIsInstance(observed_fact_without_evidence, DraftFinding)
        self.assertEqual(observed_fact_without_evidence.confidence, "S")
        self.assertFalse(observed_fact_without_evidence.requires_disposition)


# -- AC-FI-3: sealing never folds through ingest_audit -----------------------


class SealFindingsUnitTests(FindingIntakeTestCase):
    def test_seal_findings_requires_at_least_one_finding(self) -> None:
        with self.assertRaises(FindingIntakeError):
            seal_findings([], self.store)

    def test_seal_findings_uses_campaign_artifact_store_seal(self) -> None:
        finding = draft_finding(
            STATEMENT_COMPUTE_TOTAL, repo_root=self.repo_root, target="t"
        )
        self.assertIsInstance(finding, DraftFinding)
        record = seal_findings([finding], self.store)
        self.assertTrue(self.store.contains(record.digest))
        envelope = json.loads(self.store.open_bytes(record.digest))
        self.assertEqual(set(envelope), {
            "digest", "findings", "verdicts", "confirmed_good",
            "capture_coverage",
        })
        self.assertEqual(envelope["verdicts"], [])
        self.assertEqual(envelope["confirmed_good"], [])
        self.assertEqual(envelope["capture_coverage"], {})
        self.assertEqual(envelope["findings"], [finding.to_envelope()])


# -- AC-FI-4: batch seed-audit transcription is all-or-nothing --------------


class BatchTranscriptionTests(FindingIntakeTestCase):
    def test_batch_draft_findings_confidence_and_disposition(self) -> None:
        findings = draft_findings_batch(
            [STATEMENT_COMPUTE_TOTAL, STATEMENT_RENDER_WIDGET],
            repo_root=self.repo_root, target="t",
            evidence_refs_by_index={1: ("cap-1",)},
        )
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].confidence, "S")
        self.assertTrue(findings[0].requires_disposition)
        self.assertEqual(findings[1].confidence, "C+S")
        self.assertFalse(findings[1].requires_disposition)

    def test_batch_draft_findings_round_trip_real_ledger(self) -> None:
        findings = draft_findings_batch(
            [STATEMENT_COMPUTE_TOTAL, STATEMENT_RENDER_WIDGET, STATEMENT_PATH_CITED],
            repo_root=self.repo_root, target="t",
        )
        self.assertEqual(len(findings), 3)

        ledger = ConvergenceLedger(self.ledger_path)
        audit = {
            "digest": "batch-d1",
            "findings": [finding.to_envelope() for finding in findings],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        }
        summary = ledger.ingest_audit(audit)  # must not raise
        expected_keys = {(finding.file, finding.subject) for finding in findings}
        self.assertEqual(
            {tuple(key) for key in summary["opened"]}, expected_keys
        )
        self.assertEqual(ledger.open_set(), expected_keys)

    def test_batch_with_a_bad_statement_raises_finding_intake_error(self) -> None:
        with self.assertRaisesRegex(FindingIntakeError, "statement 1"):
            draft_findings_batch(
                [STATEMENT_COMPUTE_TOTAL, STATEMENT_NO_TERM],
                repo_root=self.repo_root, target="t",
            )

    def test_batch_duplicate_derived_key_raises_finding_intake_error(self) -> None:
        # Two distinct statements about the same symbol derive the same
        # (file, subject) key; ingesting the second would be a silent
        # ledger no-op, so the batch must be rejected rather than dropping
        # the second statement's text.
        with self.assertRaisesRegex(FindingIntakeError, "same key"):
            draft_findings_batch(
                [STATEMENT_COMPUTE_TOTAL, STATEMENT_COMPUTE_TOTAL_ALT],
                repo_root=self.repo_root, target="t",
            )

    def test_batch_cli_seals_one_artifact_for_n_statements(self) -> None:
        seed_file = self.root / "seed_audit.json"
        seed_file.write_text(
            json.dumps([
                STATEMENT_COMPUTE_TOTAL, STATEMENT_RENDER_WIDGET,
                STATEMENT_PATH_CITED,
            ]),
            encoding="utf-8",
        )
        argv = [
            "--batch", str(seed_file),
            "--ledger", str(self.ledger_path),
            "--campaign-root", str(self.campaign_root),
            "--repo-root", str(self.repo_root),
            "--target", "t",
        ]
        exit_code = report_finding.main(argv)
        self.assertEqual(exit_code, 0)

        objects_dir = self.campaign_root / "artifacts" / "objects"
        content_files = [
            path for path in objects_dir.iterdir() if path.suffix != ".json"
        ]
        self.assertEqual(len(content_files), 1)
        envelope = json.loads(content_files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(envelope["findings"]), 3)

        # The sealed envelope itself round-trips the real ledger, proving
        # the CLI's batch output -- not just draft_findings_batch's return
        # value -- is contract-valid.
        ledger = ConvergenceLedger(self.ledger_path)
        summary = ledger.ingest_audit(envelope)  # must not raise
        self.assertEqual(len(summary["opened"]), 3)
        self.assertEqual(
            ledger.open_set(),
            {(f["file"], f["subject"]) for f in envelope["findings"]},
        )


# -- AC-FI-3/AC-FI-5: the CLI never ingests; explicit --ledger/--campaign-root --


class ReportFindingCliTests(FindingIntakeTestCase):
    def _run(self, statement: str, **extra_args: str) -> int:
        argv = [
            statement,
            "--ledger", str(self.ledger_path),
            "--campaign-root", str(self.campaign_root),
            "--repo-root", str(self.repo_root),
            "--target", "t",
        ]
        for flag, value in extra_args.items():
            argv.extend([flag, value])
        return report_finding.main(argv)

    def test_single_statement_seals_via_artifact_store_never_ingests(self) -> None:
        exit_code = self._run(STATEMENT_COMPUTE_TOTAL)
        self.assertEqual(exit_code, 0)
        self.assertFalse(self.ledger_path.exists())
        objects_dir = self.campaign_root / "artifacts" / "objects"
        content_files = [p for p in objects_dir.iterdir() if p.suffix != ".json"]
        self.assertEqual(len(content_files), 1)

    def test_never_calls_ingest_audit_uses_explicit_ledger_and_campaign_root_args(
        self,
    ) -> None:
        ledger = ConvergenceLedger(self.ledger_path)
        ledger.ingest_audit({
            "digest": "seed",
            "findings": [{
                "file": "pkg_a/module_one.py", "subject": "compute_total",
                "required_paths": ["pkg_a/module_one.py"], "confidence": "C",
            }],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        })

        def _forbidden(self, audit_result):
            raise AssertionError("ingest_audit must never be called by the CLI")

        with patch.object(ConvergenceLedger, "ingest_audit", _forbidden):
            exit_code = self._run(STATEMENT_COMPUTE_TOTAL)
        self.assertEqual(exit_code, 0)

    def test_rerun_with_identical_input_reseals_same_digest_and_no_op(self) -> None:
        first = self._run(STATEMENT_COMPUTE_TOTAL)
        objects_dir = self.campaign_root / "artifacts" / "objects"
        before = {p: p.read_bytes() for p in objects_dir.iterdir()}

        second = self._run(STATEMENT_COMPUTE_TOTAL)
        after = {p: p.read_bytes() for p in objects_dir.iterdir()}

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(before, after)
        content_files = [p for p in objects_dir.iterdir() if p.suffix != ".json"]
        self.assertEqual(len(content_files), 1)

    def test_unsuccessful_repair_claims_not_incremented(self) -> None:
        ledger = ConvergenceLedger(self.ledger_path)
        ledger.ingest_audit({
            "digest": "seed",
            "findings": [{
                "file": "pkg_a/module_one.py", "subject": "compute_total",
                "required_paths": ["pkg_a/module_one.py"], "confidence": "C",
            }],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        })
        ledger.record_fix_claimed(("pkg_a/module_one.py", "compute_total"))
        records_before = ledger.records()

        exit_code = self._run(STATEMENT_COMPUTE_TOTAL)

        self.assertEqual(exit_code, 0)
        self.assertEqual(ledger.records(), records_before)
        self.assertFalse(
            any(record["type"] == "finding_reopened" for record in ledger.records())
        )


if __name__ == "__main__":
    unittest.main()
