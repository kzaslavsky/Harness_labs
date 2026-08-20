"""Tests for the repo-scoped finding history (EM-2 / em-history).

Covers AC-EM-5 (newest-first lineage with rulings/status/base_commit/
ordinals), AC-EM-6 (path containment vs. bare-prefix false positives),
AC-EM-7 (repository_id disagreement), AC-EM-8 (no ledger-parsing duplication
/ no ``state-ledger`` record-kind literal / ConvergenceLedgerError
propagation), and AC-EM-23 (never writes; raises before constructing a
ledger for a missing path).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from harness_labs.plangraph.convergence_ledger import (
    ConvergenceLedger,
    ConvergenceLedgerError,
)
from harness_labs.plangraph.finding_history import (
    FindingHistoryError,
    fold_campaigns,
)


def _finding(file: str, subject: str, **overrides) -> dict:
    finding = {
        "file": file, "subject": subject, "required_paths": [file],
        "confidence": "C",
    }
    finding.update(overrides)
    return finding


def _audit(digest: str, *, findings=(), confirmed_good=()) -> dict:
    return {
        "digest": digest, "findings": list(findings), "verdicts": [],
        "confirmed_good": list(confirmed_good), "capture_coverage": {},
    }


def _manifest(root: Path) -> dict[str, str]:
    """A ``{relative_path: sha256}`` manifest of every file under
    ``root``, for byte-identity comparison before/after a fold."""

    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _directories(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_dir()}


class FindingHistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _journal(self, name: str) -> Path:
        return self.root / name / "ledger.jsonl"


# -- AC-EM-5 ------------------------------------------------------------


class ForKeyLineageTests(FindingHistoryTestCase):
    KEY = ("pkg/sub/mod.py", "leaked-secret")

    def test_lineage_carries_status_rulings_label_base_commit_and_ordinals(
        self,
    ) -> None:
        older = self._journal("older-campaign")
        older_ledger = ConvergenceLedger(older)
        older_ledger.open_campaign(
            domain="d", target={"kind": "k", "digest": "x", "snapshot_path": "t.md"},
            base_commit="older-commit",
        )
        older_ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        older_ledger.record_ruling(
            self.KEY, disposition="waive", statement="accepted risk, old campaign",
        )

        newer = self._journal("newer-campaign")
        newer_ledger = ConvergenceLedger(newer)
        newer_ledger.open_campaign(
            domain="d", target={"kind": "k", "digest": "y", "snapshot_path": "t.md"},
            base_commit="newer-commit",
        )
        newer_ledger.ingest_audit(_audit("d2", findings=[_finding(*self.KEY)]))
        newer_ledger.record_ruling(
            self.KEY, disposition="require_repair", statement="fix it for real",
        )

        history = fold_campaigns([
            (older, "older-label", "repo-1"),
            (newer, "newer-label", "repo-1"),
        ])
        lineage = history.for_key(*self.KEY)

        self.assertEqual(len(lineage), 2)
        # newest-entry-first: the second-declared entry comes first.
        self.assertEqual(lineage[0].campaign_label, "newer-label")
        self.assertEqual(lineage[1].campaign_label, "older-label")

        newest, oldest = lineage
        self.assertEqual(newest.base_commit, "newer-commit")
        self.assertEqual(oldest.base_commit, "older-commit")

        # require_repair does not close the key.
        self.assertEqual(newest.status, "open")
        self.assertEqual(len(newest.rulings), 1)
        self.assertEqual(newest.rulings[0].disposition, "require_repair")
        self.assertEqual(newest.rulings[0].statement, "fix it for real")

        # waive folds to the terminal status "excluded".
        self.assertEqual(oldest.status, "excluded")
        self.assertEqual(len(oldest.rulings), 1)
        self.assertEqual(oldest.rulings[0].disposition, "waive")
        self.assertEqual(oldest.rulings[0].statement, "accepted risk, old campaign")

        # journal ordinals are the when-learned order within each
        # campaign's own journal: campaign_opened(0), finding_opened(1),
        # audit_ingested(2), then finding_ruled(3).
        self.assertEqual(newest.ordinals, (1, 3))
        self.assertEqual(oldest.ordinals, (1, 3))
        self.assertLess(newest.ordinals[0], newest.ordinals[-1])

    def test_for_key_returns_empty_tuple_for_an_unknown_key(self) -> None:
        journal = self._journal("campaign")
        ledger = ConvergenceLedger(journal)
        ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        history = fold_campaigns([(journal, "label", "repo-1")])
        self.assertEqual(history.for_key("nope.py", "nope"), ())


# -- AC-EM-6 ------------------------------------------------------------


class ForPathsContainmentTests(FindingHistoryTestCase):
    def test_directory_prefix_and_exact_match_without_bare_string_false_positive(
        self,
    ) -> None:
        journal = self._journal("campaign")
        ledger = ConvergenceLedger(journal)
        contained_key = ("pkg/sub/mod.py", "finding-a")
        exact_key = ("pkg/exact.py", "finding-b")
        sibling_key = ("pkg/subx/mod.py", "finding-c")
        ledger.ingest_audit(_audit("d1", findings=[
            _finding(*contained_key),
            _finding(*exact_key),
            _finding(*sibling_key),
        ]))
        # a key that never received a finding_opened record (a bare
        # unconfirmed "watch" entry) has no recorded required_paths.
        watch_key = ("pkg/watch.py", "finding-d")
        ledger.ingest_audit(_audit("d2", confirmed_good=[
            {"key": list(watch_key), "reason": "looked fine, unconfirmed"},
        ]))

        history = fold_campaigns([(journal, "label", "repo-1")])

        matched_keys = {entry.key for entry in history.for_paths(["pkg/sub"])}
        self.assertIn(contained_key, matched_keys)
        self.assertNotIn(sibling_key, matched_keys)

        exact_matched = {entry.key for entry in history.for_paths(["pkg/exact.py"])}
        self.assertIn(exact_key, exact_matched)

        # a key with no recorded required_paths is excluded, not raised on.
        self.assertNotIn(watch_key, matched_keys)
        self.assertEqual(history.for_paths(["pkg/watch.py"]), ())


# -- AC-EM-7 --------------------------------------------------------------


class RepositoryIdGuardTests(FindingHistoryTestCase):
    def test_disagreeing_repository_ids_raise_naming_both(self) -> None:
        first = self._journal("campaign-1")
        second = self._journal("campaign-2")
        ConvergenceLedger(first).ingest_audit(
            _audit("d1", findings=[_finding("a.py", "s1")])
        )
        ConvergenceLedger(second).ingest_audit(
            _audit("d1", findings=[_finding("b.py", "s2")])
        )

        with self.assertRaisesRegex(FindingHistoryError, "repo-alpha") as ctx:
            fold_campaigns([
                (first, "label-1", "repo-alpha"),
                (second, "label-2", "repo-beta"),
            ])
        self.assertIn("repo-beta", str(ctx.exception))


# -- AC-EM-8 ----------------------------------------------------------------


class LedgerAccessorOnlyTests(FindingHistoryTestCase):
    def test_no_state_ledger_record_kind_string_literal_in_source(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[1]
            / "harness_labs" / "plangraph" / "finding_history.py"
        )
        source = source_path.read_text(encoding="utf-8")
        for literal in (
            '"campaign_opened"', "'campaign_opened'",
            '"finding_opened"', "'finding_opened'",
            '"finding_fix_claimed"', "'finding_fix_claimed'",
            '"finding_fixed"', "'finding_fixed'",
            '"finding_reopened"', "'finding_reopened'",
            '"finding_invalidated"', "'finding_invalidated'",
            '"finding_ruled"', "'finding_ruled'",
            '"confirmed_good"', "'confirmed_good'",
            '"target_amended"', "'target_amended'",
            '"capture_coverage"', "'capture_coverage'",
            '"audit_ingested"', "'audit_ingested'",
        ):
            self.assertNotIn(
                literal, source,
                f"finding_history.py contains a record-kind string literal: {literal}",
            )

    def test_a_journal_the_ledger_rejects_raises_convergence_ledger_error(
        self,
    ) -> None:
        journal = self._journal("corrupt-campaign")
        journal.parent.mkdir(parents=True)
        journal.write_text("not-json-and-not-a-record\n", encoding="utf-8")

        with self.assertRaises(ConvergenceLedgerError):
            fold_campaigns([(journal, "label", "repo-1")])


# -- AC-EM-23 -----------------------------------------------------------


class NoWriteTests(FindingHistoryTestCase):
    def test_missing_journal_path_raises_before_constructing_any_ledger(
        self,
    ) -> None:
        missing = self._journal("never-created")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

        with self.assertRaisesRegex(FindingHistoryError, "does not exist"):
            fold_campaigns([(missing, "label", "repo-1")])

        # the ledger's own `open` would have created the parent directory
        # and the journal file; folding must not have touched either.
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

    def test_missing_entry_among_valid_entries_still_raises_before_any_ledger_touch(
        self,
    ) -> None:
        existing = self._journal("existing-campaign")
        ConvergenceLedger(existing).ingest_audit(
            _audit("d1", findings=[_finding("a.py", "s1")])
        )
        before = existing.read_bytes()
        missing = self._journal("never-created")

        with self.assertRaises(FindingHistoryError):
            fold_campaigns([
                (existing, "existing-label", "repo-1"),
                (missing, "missing-label", "repo-1"),
            ])

        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assertEqual(existing.read_bytes(), before)

    def test_successful_fold_leaves_journal_root_byte_identical(self) -> None:
        first = self._journal("first-campaign")
        first_ledger = ConvergenceLedger(first)
        first_ledger.open_campaign(
            domain="d", target={"kind": "k", "digest": "x", "snapshot_path": "t.md"},
            base_commit="commit-1",
        )
        key = ("a.py", "finding-a")
        first_ledger.ingest_audit(_audit("d1", findings=[_finding(*key)]))
        first_ledger.record_ruling(key, disposition="waive", statement="ok")

        second = self._journal("second-campaign")
        ConvergenceLedger(second).ingest_audit(
            _audit("d1", findings=[_finding("b.py", "finding-b")])
        )

        manifest_before = _manifest(self.root)
        directories_before = _directories(self.root)

        history = fold_campaigns([
            (first, "first-label", "repo-1"),
            (second, "second-label", "repo-1"),
        ])
        # a genuinely successful fold, not a vacuous no-op.
        self.assertEqual(len(history.for_key(*key)), 1)

        self.assertEqual(_manifest(self.root), manifest_before)
        self.assertEqual(_directories(self.root), directories_before)


if __name__ == "__main__":
    unittest.main()
