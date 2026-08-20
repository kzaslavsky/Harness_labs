"""Tests for the convergence campaign ledger (CC-01).

Covers every AC-CC01-* acceptance criterion plus the ``tests-ledger``
checklist: round-trip; ruling semantics; ``fix_claimed`` never terminal;
verdict semantics; ``base_rebase`` demotion; repair-attempt stall; watch
admission; ingest validation; idempotent ingest; lock safety.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import tempfile
import unittest

from harness_labs.core.convergence_contract import (
    CAPTURE_CELL_STATUSES,
    RULING_DISPOSITIONS,
    VERDICT_KINDS,
)
from harness_labs.plangraph.convergence_ledger import (
    ConvergenceLedger,
    ConvergenceLedgerError,
    RECORD_KIND_AUDIT_INGESTED,
    RECORD_KIND_CAMPAIGN_OPENED,
    RECORD_KIND_CAPTURE_COVERAGE,
    RECORD_KIND_CONFIRMED_GOOD,
    RECORD_KIND_FINDING_FIX_CLAIMED,
    RECORD_KIND_FINDING_FIXED,
    RECORD_KIND_FINDING_INVALIDATED,
    RECORD_KIND_FINDING_OPENED,
    RECORD_KIND_FINDING_REOPENED,
    RECORD_KIND_FINDING_RULED,
    RECORD_KIND_TARGET_AMENDED,
)


def _finding(file: str, subject: str, **overrides) -> dict:
    finding = {
        "file": file, "subject": subject, "required_paths": [file],
        "confidence": "C",
    }
    finding.update(overrides)
    return finding


def _audit(
    digest: str,
    *,
    findings=(),
    verdicts=(),
    confirmed_good=(),
    capture_coverage=None,
) -> dict:
    return {
        "digest": digest,
        "findings": list(findings),
        "verdicts": list(verdicts),
        "confirmed_good": list(confirmed_good),
        "capture_coverage": capture_coverage or {},
    }


def _observed_fixed(key, *, capture_cell="cell-1", assertion="assert-1"):
    return {
        "key": list(key), "verdict": "observed_fixed",
        "capture_cell": capture_cell, "assertion": assertion,
    }


def _reopened(key):
    return {"key": list(key), "verdict": "reopened"}


def _unobserved(key):
    return {"key": list(key), "verdict": "unobserved"}


def _invalidated(key):
    return {"key": list(key), "verdict": "invalidated"}


def _ingest_in_subprocess(path_str: str, index: int) -> None:
    key = (f"m{index}/file.py", f"finding-{index}")
    ConvergenceLedger(Path(path_str)).ingest_audit(
        _audit(f"digest-{index}", findings=[_finding(*key)])
    )


class ConvergenceLedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "campaign" / "ledger.jsonl"
        self.ledger = ConvergenceLedger(self.path)


class VocabularyTests(unittest.TestCase):
    def test_closed_vocabularies_match_contract(self) -> None:
        self.assertEqual(
            VERDICT_KINDS,
            frozenset({"observed_fixed", "reopened", "unobserved", "invalidated"}),
        )
        self.assertEqual(
            RULING_DISPOSITIONS,
            frozenset({"waive", "require_repair", "amend_criterion"}),
        )
        self.assertEqual(
            CAPTURE_CELL_STATUSES, frozenset({"ok", "unreachable", "unstable"})
        )


# -- AC-CC01-1: ingest-time finding-contract validation ---------------------


class IngestValidationTests(ConvergenceLedgerTestCase):
    def test_missing_file_rejected(self) -> None:
        finding = _finding("a.py", "s1")
        del finding["file"]
        with self.assertRaisesRegex(ConvergenceLedgerError, "file"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_empty_file_rejected(self) -> None:
        finding = _finding("   ", "s1", required_paths=["   "])
        with self.assertRaisesRegex(ConvergenceLedgerError, "file"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_missing_subject_rejected(self) -> None:
        finding = _finding("a.py", "s1")
        del finding["subject"]
        with self.assertRaisesRegex(ConvergenceLedgerError, "subject"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_empty_subject_rejected(self) -> None:
        finding = _finding("a.py", "   ")
        with self.assertRaisesRegex(ConvergenceLedgerError, "subject"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_missing_required_paths_rejected(self) -> None:
        finding = _finding("a.py", "s1")
        del finding["required_paths"]
        with self.assertRaisesRegex(ConvergenceLedgerError, "required_paths"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_empty_required_paths_rejected(self) -> None:
        finding = _finding("a.py", "s1", required_paths=[])
        with self.assertRaisesRegex(ConvergenceLedgerError, "required_paths"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_file_not_a_member_of_required_paths_rejected(self) -> None:
        finding = _finding("a.py", "s1", required_paths=["other.py"])
        with self.assertRaisesRegex(ConvergenceLedgerError, "required_paths"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))
        self.assertEqual(self.ledger.records(), ())

    def test_one_bad_finding_fails_the_whole_ingest_atomically(self) -> None:
        good = _finding("good.py", "s-good")
        bad = _finding("bad.py", "s-bad", required_paths=[])
        with self.assertRaises(ConvergenceLedgerError):
            self.ledger.ingest_audit(_audit("d1", findings=[good, bad]))
        self.assertEqual(self.ledger.open_set(), frozenset())
        self.assertEqual(self.ledger.records(), ())

    def test_invalid_verdict_kind_rejected(self) -> None:
        with self.assertRaisesRegex(ConvergenceLedgerError, "verdict"):
            self.ledger.ingest_audit(
                _audit("d1", verdicts=[{"key": ["a.py", "s1"], "verdict": "bogus"}])
            )

    def test_invalid_capture_coverage_status_rejected(self) -> None:
        with self.assertRaisesRegex(ConvergenceLedgerError, "capture_coverage"):
            self.ledger.ingest_audit(
                _audit("d1", capture_coverage={"cell-1": "flaky"})
            )

    def test_confirmed_good_bad_key_rejected(self) -> None:
        with self.assertRaisesRegex(ConvergenceLedgerError, "key"):
            self.ledger.ingest_audit(
                _audit("d1", confirmed_good=[{"key": ["only-one-part"]}])
            )

    def test_invalid_severity_rejected(self) -> None:
        finding = _finding("a.py", "s1", severity="catastrophic")
        with self.assertRaisesRegex(ConvergenceLedgerError, "severity"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))

    def test_non_bool_requires_disposition_rejected(self) -> None:
        finding = _finding("a.py", "s1", requires_disposition="yes")
        with self.assertRaisesRegex(ConvergenceLedgerError, "requires_disposition"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))

    def test_non_string_evidence_refs_rejected(self) -> None:
        finding = _finding("a.py", "s1", evidence_refs=[123])
        with self.assertRaisesRegex(ConvergenceLedgerError, "evidence_refs"):
            self.ledger.ingest_audit(_audit("d1", findings=[finding]))


# -- AC-CC01-2: finding_fixed / unobserved / success ------------------------


class VerdictSemanticsTests(ConvergenceLedgerTestCase):
    KEY = ("service/api.py", "endpoint-timeout")

    def _open_and_claim(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        self.ledger.record_fix_claimed(self.KEY)

    def test_observed_fixed_without_capture_cell_citation_rejected(self) -> None:
        self._open_and_claim()
        bad_verdict = {"key": list(self.KEY), "verdict": "observed_fixed"}
        with self.assertRaisesRegex(ConvergenceLedgerError, "capture_cell"):
            self.ledger.ingest_audit(_audit("d2", verdicts=[bad_verdict]))
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")

    def test_observed_fixed_without_assertion_rejected(self) -> None:
        self._open_and_claim()
        bad_verdict = {
            "key": list(self.KEY), "verdict": "observed_fixed",
            "capture_cell": "cell-1",
        }
        with self.assertRaisesRegex(ConvergenceLedgerError, "assertion"):
            self.ledger.ingest_audit(_audit("d2", verdicts=[bad_verdict]))

    def test_observed_fixed_with_citation_closes_the_key(self) -> None:
        self._open_and_claim()
        self.ledger.ingest_audit(
            _audit(
                "d2",
                verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.assertEqual(self.ledger.key_status(self.KEY), "fixed")
        self.assertNotIn(self.KEY, self.ledger.open_set())

    def test_unmentioned_prior_key_is_unobserved_and_blocks_success(self) -> None:
        self._open_and_claim()
        summary = self.ledger.ingest_audit(_audit("d2"))
        self.assertEqual(summary["unobserved"], [list(self.KEY)])
        self.assertFalse(self.ledger.success())

    def test_success_true_once_every_prior_key_is_resolved(self) -> None:
        self._open_and_claim()
        self.ledger.ingest_audit(
            _audit(
                "d2",
                verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.assertTrue(self.ledger.success())

    def test_success_false_before_any_audit_is_ingested(self) -> None:
        self.assertFalse(self.ledger.success())

    def test_explicit_unobserved_verdict_still_blocks_success(self) -> None:
        self._open_and_claim()
        summary = self.ledger.ingest_audit(
            _audit("d2", verdicts=[_unobserved(self.KEY)])
        )
        self.assertEqual(summary["unobserved"], [list(self.KEY)])
        self.assertFalse(self.ledger.success())

    def test_reopened_verdict_against_fixed_key_reopens_it(self) -> None:
        self._open_and_claim()
        self.ledger.ingest_audit(
            _audit(
                "d2", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.assertEqual(self.ledger.key_status(self.KEY), "fixed")
        self.ledger.ingest_audit(_audit("d3", verdicts=[_reopened(self.KEY)]))
        self.assertEqual(self.ledger.key_status(self.KEY), "open")

    def test_verdict_citing_unknown_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConvergenceLedgerError, "unknown key"):
            self.ledger.ingest_audit(
                _audit("d1", verdicts=[_reopened(("nope.py", "nope"))])
            )

    def test_unknown_capture_cell_citation_cannot_write_finding_fixed(self) -> None:
        self._open_and_claim()
        verdict = {
            "key": list(self.KEY), "verdict": "observed_fixed",
            "capture_cell": "never-recorded", "assertion": "assert-1",
        }
        summary = self.ledger.ingest_audit(_audit("d2", verdicts=[verdict]))
        self.assertEqual(summary["fixed"], [])
        self.assertEqual(summary["blocked_unknown_cell"], [list(self.KEY)])
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")


# -- AC-CC01-3: ruling dispositions ------------------------------------------


class RulingSemanticsTests(ConvergenceLedgerTestCase):
    KEY = ("core/session.py", "leaked-token")

    def setUp(self) -> None:
        super().setUp()
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))

    def test_invalid_disposition_rejected(self) -> None:
        with self.assertRaisesRegex(ConvergenceLedgerError, "disposition"):
            self.ledger.record_ruling(
                self.KEY, disposition="ignore", statement="nope"
            )

    def test_ruling_an_unknown_key_rejected(self) -> None:
        with self.assertRaisesRegex(ConvergenceLedgerError, "unknown key"):
            self.ledger.record_ruling(
                ("nope.py", "nope"), disposition="waive", statement="n/a"
            )

    def test_waive_adds_to_exclusion_set(self) -> None:
        self.ledger.record_ruling(
            self.KEY, disposition="waive", statement="accepted risk"
        )
        self.assertIn(self.KEY, self.ledger.exclusion_set())
        self.assertNotIn(self.KEY, self.ledger.open_set())

    def test_require_repair_keeps_key_open_and_excluded_from_exclusion_set(
        self,
    ) -> None:
        self.ledger.record_ruling(
            self.KEY, disposition="require_repair", statement="fix it properly"
        )
        self.assertIn(self.KEY, self.ledger.open_set())
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())

    def test_amend_criterion_does_not_enter_exclusion_set(self) -> None:
        self.ledger.record_ruling(
            self.KEY, disposition="amend_criterion",
            statement="criterion narrowed to intended scope",
        )
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())
        self.assertNotIn(self.KEY, self.ledger.open_set())

    def test_amendment_ratio_counts_amend_criterion_among_closed_keys(
        self,
    ) -> None:
        other_key = ("core/other.py", "other-finding")
        self.ledger.ingest_audit(_audit("d2", findings=[_finding(*other_key)]))
        self.ledger.record_ruling(
            self.KEY, disposition="amend_criterion", statement="criterion amended"
        )
        self.ledger.record_ruling(
            other_key, disposition="waive", statement="accepted"
        )
        self.assertAlmostEqual(self.ledger.amendment_ratio(), 0.5)

    def test_amendment_ratio_is_zero_with_no_closed_keys(self) -> None:
        self.assertEqual(self.ledger.amendment_ratio(), 0.0)

    def test_fix_claimed_on_waived_key_rejected(self) -> None:
        self.ledger.record_ruling(
            self.KEY, disposition="waive", statement="accepted risk"
        )
        with self.assertRaisesRegex(ConvergenceLedgerError, "already"):
            self.ledger.record_fix_claimed(self.KEY)
        self.assertIn(self.KEY, self.ledger.exclusion_set())

    def test_fix_claimed_on_amended_key_rejected(self) -> None:
        self.ledger.record_ruling(
            self.KEY, disposition="amend_criterion", statement="scope narrowed"
        )
        with self.assertRaisesRegex(ConvergenceLedgerError, "already"):
            self.ledger.record_fix_claimed(self.KEY)


# -- AC-CC01-4: stall detection ----------------------------------------------


class StallDetectionTests(ConvergenceLedgerTestCase):
    KEY = ("worker/pool.py", "deadlock")

    def test_two_unsuccessful_repair_claims_stall(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(_audit("d2", verdicts=[_reopened(self.KEY)]))
        self.assertFalse(self.ledger.is_stalled())
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(_audit("d3", verdicts=[_reopened(self.KEY)]))
        self.assertIn(self.KEY, self.ledger.stalled_keys())
        self.assertTrue(self.ledger.is_stalled())

    def test_fixed_reopened_fixed_reopened_cycle_stalls(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(
            _audit(
                "d2", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.assertEqual(self.ledger.key_status(self.KEY), "fixed")
        # A finding re-emitted against an already-fixed key reopens it
        # (contracts-finding re-emission).
        self.ledger.ingest_audit(_audit("d3", findings=[_finding(*self.KEY)]))
        self.assertEqual(self.ledger.key_status(self.KEY), "open")
        self.assertFalse(self.ledger.is_stalled())
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(
            _audit(
                "d4", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.ledger.ingest_audit(_audit("d5", findings=[_finding(*self.KEY)]))
        self.assertIn(self.KEY, self.ledger.stalled_keys())

    def test_finding_reemitted_against_fix_claimed_key_counts_as_unsuccessful_repair_claim(
        self,
    ) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(_audit("d2", findings=[_finding(*self.KEY)]))
        self.assertEqual(self.ledger.key_status(self.KEY), "open")
        self.assertFalse(self.ledger.is_stalled())
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(_audit("d3", findings=[_finding(*self.KEY)]))
        self.assertIn(self.KEY, self.ledger.stalled_keys())

    def test_open_across_two_audits_with_no_intervening_fix_claimed_does_not_stall(
        self,
    ) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        self.ledger.ingest_audit(_audit("d2"))
        self.ledger.ingest_audit(_audit("d3"))
        self.assertEqual(self.ledger.key_status(self.KEY), "open")
        self.assertFalse(self.ledger.is_stalled())
        self.assertNotIn(self.KEY, self.ledger.stalled_keys())


# -- AC-CC01-5: idempotent ingest and lock safety ----------------------------


class IdempotencyAndLockingTests(ConvergenceLedgerTestCase):
    KEY = ("db/migrations.py", "missing-index")

    def test_reingesting_the_same_digest_changes_no_state(self) -> None:
        audit = _audit("d1", findings=[_finding(*self.KEY)])
        first = self.ledger.ingest_audit(audit)
        self.assertFalse(first["idempotent"])
        before = self.ledger.records()
        second = self.ledger.ingest_audit(audit)
        self.assertTrue(second["idempotent"])
        after = self.ledger.records()
        self.assertEqual(before, after)

    def test_concurrent_appends_from_separate_processes_replay_to_one_coherent_state(
        self,
    ) -> None:
        keys = [(f"m{i}/file.py", f"finding-{i}") for i in range(6)]

        with ProcessPoolExecutor(max_workers=6) as pool:
            list(
                pool.map(
                    _ingest_in_subprocess,
                    [str(self.path)] * len(keys),
                    range(len(keys)),
                )
            )

        raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(raw_lines)
        for line in raw_lines:
            self.assertTrue(line.strip())

        # Two independent replays of the journal the processes produced
        # must agree on every derived view -- the journal replays to one
        # coherent state regardless of which process reads it back.
        first_replay = ConvergenceLedger(self.path)
        second_replay = ConvergenceLedger(self.path)
        self.assertEqual(first_replay.open_set(), second_replay.open_set())
        self.assertEqual(first_replay.records(), second_replay.records())

        self.assertEqual(first_replay.open_set(), frozenset(keys))
        # Every finding_opened plus every audit_ingested marker landed
        # exactly once: no interleaved or lost writes under contention.
        record_types = [record["type"] for record in first_replay.records()]
        self.assertEqual(record_types.count("finding_opened"), len(keys))
        self.assertEqual(record_types.count("audit_ingested"), len(keys))


# -- AC-CC01-6: base_rebase demotion -----------------------------------------


class BaseRebaseTests(ConvergenceLedgerTestCase):
    KEY = ("infra/deploy.py", "flaky-rollout")

    def _fix_the_key(self, digest: str) -> None:
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(
            _audit(
                digest, verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )

    def setUp(self) -> None:
        super().setUp()
        self.ledger.ingest_audit(_audit("d0", findings=[_finding(*self.KEY)]))

    def test_base_rebase_demotes_fixed_key_to_fix_claimed(self) -> None:
        self._fix_the_key("d1")
        self.assertEqual(self.ledger.key_status(self.KEY), "fixed")
        self.ledger.record_base_rebase()
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")

    def test_base_rebase_demotions_are_stall_exempt(self) -> None:
        self._fix_the_key("d1")
        self.ledger.record_base_rebase()
        self.ledger.ingest_audit(
            _audit(
                "d2", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.ledger.record_base_rebase()
        # Two fixed -> base_rebase demotions look superficially like a
        # fixed/reopened cycle but must never count toward stall.
        self.assertFalse(self.ledger.is_stalled())
        self.assertNotIn(self.KEY, self.ledger.stalled_keys())

    def test_reopened_verdict_against_rebase_demoted_key_is_stall_exempt(self) -> None:
        self._fix_the_key("d1")
        self.ledger.record_base_rebase()
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")
        self.ledger.ingest_audit(_audit("d2", verdicts=[_reopened(self.KEY)]))
        self.assertEqual(self.ledger.key_status(self.KEY), "open")
        self.assertFalse(self.ledger.is_stalled())
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(
            _audit(
                "d3", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.ledger.record_base_rebase()
        self.ledger.ingest_audit(_audit("d4", verdicts=[_reopened(self.KEY)]))
        self.assertFalse(self.ledger.is_stalled())
        self.assertNotIn(self.KEY, self.ledger.stalled_keys())

    def test_base_rebase_only_demotes_currently_fixed_keys(self) -> None:
        other_key = ("infra/other.py", "still-open")
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*other_key)]))
        self._fix_the_key("d2")
        self.ledger.record_base_rebase()
        self.assertEqual(self.ledger.key_status(other_key), "open")
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")


# -- AC-CC01-7: watch admission ----------------------------------------------


class ConfirmedGoodTests(ConvergenceLedgerTestCase):
    KEY = ("ui/theme.py", "contrast-ratio")

    def test_confirmed_good_without_assertion_is_watch_not_excluded(self) -> None:
        summary = self.ledger.ingest_audit(
            _audit(
                "d1",
                confirmed_good=[{"key": list(self.KEY), "reason": "looked fine"}],
            )
        )
        self.assertEqual(summary["watch"], [list(self.KEY)])
        self.assertEqual(summary["excluded"], [])
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())

    def test_finding_keyed_to_a_watch_entry_routes_through_open_set(self) -> None:
        self.ledger.ingest_audit(
            _audit(
                "d1",
                confirmed_good=[{"key": list(self.KEY), "reason": "looked fine"}],
            )
        )
        self.ledger.ingest_audit(_audit("d2", findings=[_finding(*self.KEY)]))
        self.assertIn(self.KEY, self.ledger.open_set())
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())

    def test_confirmed_good_with_machine_checkable_assertion_is_excluded(
        self,
    ) -> None:
        summary = self.ledger.ingest_audit(
            _audit(
                "d1",
                confirmed_good=[{
                    "key": list(self.KEY),
                    "assertion": {"kind": "test_id", "referent": "tests/x.py::y"},
                    "reason": "covered by regression test",
                }],
            )
        )
        self.assertEqual(summary["excluded"], [list(self.KEY)])
        self.assertIn(self.KEY, self.ledger.exclusion_set())

    def test_confirmed_good_assertion_without_recognized_kind_is_watch_not_excluded(
        self,
    ) -> None:
        summary = self.ledger.ingest_audit(
            _audit(
                "d1",
                confirmed_good=[{
                    "key": list(self.KEY),
                    "assertion": {"kind": "note"},
                    "reason": "looked fine",
                }],
            )
        )
        self.assertEqual(summary["watch"], [list(self.KEY)])
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())

    def test_confirmed_good_assertion_without_referent_is_watch_not_excluded(
        self,
    ) -> None:
        summary = self.ledger.ingest_audit(
            _audit(
                "d1",
                confirmed_good=[{
                    "key": list(self.KEY),
                    "assertion": {"kind": "test_id"},
                    "reason": "looked fine",
                }],
            )
        )
        self.assertEqual(summary["watch"], [list(self.KEY)])
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())


# -- AC-CC01-8: unstable capture cells block finding_fixed -------------------


class UnstableCaptureCellTests(ConvergenceLedgerTestCase):
    KEY = ("net/retry.py", "backoff-jitter")

    def setUp(self) -> None:
        super().setUp()
        self.ledger.ingest_audit(_audit("d0", findings=[_finding(*self.KEY)]))
        self.ledger.record_fix_claimed(self.KEY)

    def test_unstable_cell_verdict_cannot_write_finding_fixed(self) -> None:
        summary = self.ledger.ingest_audit(
            _audit(
                "d1", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "unstable"},
            )
        )
        self.assertEqual(summary["fixed"], [])
        self.assertEqual(summary["blocked_unstable"], [list(self.KEY)])
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")

    def test_key_reaches_fixed_once_reobserved_on_a_stable_cell(self) -> None:
        self.ledger.ingest_audit(
            _audit(
                "d1", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "unstable"},
            )
        )
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")
        self.ledger.ingest_audit(
            _audit(
                "d2", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.assertEqual(self.ledger.key_status(self.KEY), "fixed")

    def test_unstable_status_carried_from_a_prior_audits_capture_coverage(
        self,
    ) -> None:
        self.ledger.ingest_audit(_audit("d1", capture_coverage={"cell-1": "unstable"}))
        verdict = {
            "key": list(self.KEY), "verdict": "observed_fixed",
            "capture_cell": "cell-1", "assertion": "assert-1",
        }
        self.ledger.ingest_audit(_audit("d2", verdicts=[verdict]))
        self.assertEqual(self.ledger.key_status(self.KEY), "fix_claimed")


# -- invalidated verdicts ----------------------------------------------------


class InvalidatedVerdictTests(ConvergenceLedgerTestCase):
    KEY = ("payments/charge.py", "race-condition")

    def setUp(self) -> None:
        super().setUp()
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))

    def test_invalidated_verdict_closes_the_key(self) -> None:
        self.ledger.ingest_audit(
            _audit("d2", verdicts=[_invalidated(self.KEY)])
        )
        self.assertEqual(self.ledger.key_status(self.KEY), "invalidated")
        self.assertNotIn(self.KEY, self.ledger.open_set())
        self.assertNotIn(self.KEY, self.ledger.exclusion_set())

    def test_invalidated_key_counts_toward_amendment_ratio_denominator(self) -> None:
        other_key = ("payments/other.py", "other-finding")
        self.ledger.ingest_audit(_audit("d2", findings=[_finding(*other_key)]))
        self.ledger.record_ruling(
            other_key, disposition="amend_criterion", statement="criterion narrowed"
        )
        self.ledger.ingest_audit(
            _audit("d3", verdicts=[_invalidated(self.KEY)])
        )
        self.assertAlmostEqual(self.ledger.amendment_ratio(), 0.5)


# -- Round-trip / durability -------------------------------------------------


class RoundTripTests(ConvergenceLedgerTestCase):
    def test_ledger_state_survives_a_fresh_instance_over_the_same_path(self) -> None:
        key = ("service/health.py", "flapping-check")
        self.ledger.open_campaign(
            domain="ui-fidelity",
            target={"kind": "design-file", "digest": "sha256:" + "a" * 64,
                    "snapshot_path": "target.md"},
            base_commit="deadbeef",
        )
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*key)]))
        self.ledger.record_fix_claimed(key)
        self.ledger.ingest_audit(
            _audit(
                "d2", verdicts=[_observed_fixed(key)],
                capture_coverage={"cell-1": "ok"},
            )
        )

        reopened = ConvergenceLedger(self.path)
        self.assertEqual(reopened.key_status(key), "fixed")
        self.assertTrue(reopened.success())
        self.assertNotIn(key, reopened.open_set())
        self.assertEqual(reopened.coverage_state(), {"cell-1": "ok"})
        self.assertEqual(len(reopened.records()), len(self.ledger.records()))

    def test_open_campaign_is_recorded_once(self) -> None:
        target = {"kind": "design-file", "digest": "x", "snapshot_path": "t.md"}
        self.ledger.open_campaign(
            domain="ui-fidelity", target=target, base_commit="deadbeef",
        )
        with self.assertRaisesRegex(ConvergenceLedgerError, "already recorded"):
            self.ledger.open_campaign(
                domain="ui-fidelity", target=target, base_commit="deadbeef",
            )

    def test_open_campaign_requires_target_kind_digest_and_snapshot_path(
        self,
    ) -> None:
        base_target = {
            "kind": "design-file", "digest": "x", "snapshot_path": "t.md",
        }
        for missing_field in ("kind", "digest", "snapshot_path"):
            target = dict(base_target)
            del target[missing_field]
            ledger = ConvergenceLedger(self.path.parent / f"missing-{missing_field}.jsonl")
            with self.assertRaisesRegex(ConvergenceLedgerError, missing_field):
                ledger.open_campaign(
                    domain="ui-fidelity", target=target, base_commit="deadbeef",
                )

    def test_target_amendment_without_invalidation_scope_sets_blocked_state(
        self,
    ) -> None:
        self.ledger.open_campaign(
            domain="ui-fidelity",
            target={"kind": "design-file", "digest": "x", "snapshot_path": "t.md"},
            base_commit="deadbeef",
        )
        self.assertFalse(self.ledger.is_blocked())
        self.ledger.record_target_amendment(digest="y", invalidation_scope=None)
        self.assertTrue(self.ledger.is_blocked())

    def test_target_amendment_with_stated_scope_clears_blocked_state(self) -> None:
        key = ("service/health.py", "flapping-check")
        self.ledger.open_campaign(
            domain="ui-fidelity",
            target={"kind": "design-file", "digest": "x", "snapshot_path": "t.md"},
            base_commit="deadbeef",
        )
        self.ledger.record_target_amendment(digest="y", invalidation_scope=None)
        self.assertTrue(self.ledger.is_blocked())
        self.ledger.record_target_amendment(
            digest="z", invalidation_scope=[list(key)],
        )
        self.assertFalse(self.ledger.is_blocked())


# -- em-history: named record-kind constants + key_lineage accessor --------


class RecordKindConstantsTests(unittest.TestCase):
    def test_named_constants_match_the_existing_record_type_vocabulary(self) -> None:
        self.assertEqual(
            {
                RECORD_KIND_CAMPAIGN_OPENED, RECORD_KIND_FINDING_OPENED,
                RECORD_KIND_FINDING_FIX_CLAIMED, RECORD_KIND_FINDING_FIXED,
                RECORD_KIND_FINDING_REOPENED, RECORD_KIND_FINDING_INVALIDATED,
                RECORD_KIND_FINDING_RULED, RECORD_KIND_CONFIRMED_GOOD,
                RECORD_KIND_TARGET_AMENDED, RECORD_KIND_CAPTURE_COVERAGE,
                RECORD_KIND_AUDIT_INGESTED,
            },
            ConvergenceLedger.RECORD_TYPES,
        )
        # no new record kind: exactly the eleven pre-existing kinds.
        self.assertEqual(len(ConvergenceLedger.RECORD_TYPES), 11)


class KeyLineageAccessorTests(ConvergenceLedgerTestCase):
    KEY = ("svc/handler.py", "unbounded-retry")

    def test_key_lineage_groups_records_by_key_with_journal_ordinals(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        self.ledger.record_ruling(
            self.KEY, disposition="waive", statement="accepted risk"
        )
        lineage = self.ledger.key_lineage()
        self.assertIn(self.KEY, lineage)
        records = lineage[self.KEY]
        record_types = [record["type"] for record in records]
        self.assertEqual(
            record_types, [RECORD_KIND_FINDING_OPENED, RECORD_KIND_FINDING_RULED]
        )
        # ordinals are each record's index into records(), i.e. strictly
        # increasing when-learned order.
        ordinals = [record["ordinal"] for record in records]
        self.assertEqual(ordinals, sorted(ordinals))
        self.assertEqual(len(set(ordinals)), len(ordinals))

    def test_key_lineage_excludes_campaign_level_records(self) -> None:
        self.ledger.open_campaign(
            domain="d", target={"kind": "k", "digest": "x", "snapshot_path": "t.md"},
            base_commit="deadbeef",
        )
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        lineage = self.ledger.key_lineage()
        for records in lineage.values():
            for record in records:
                self.assertNotEqual(record["type"], RECORD_KIND_CAMPAIGN_OPENED)

    def test_key_lineage_is_read_only_and_leaves_records_unchanged(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding(*self.KEY)]))
        before = self.ledger.records()
        self.ledger.key_lineage()
        self.ledger.key_lineage()
        after = self.ledger.records()
        self.assertEqual(before, after)

    def test_base_rebase_reopened_record_reaches_every_affected_key_lineage(
        self,
    ) -> None:
        other_key = ("svc/other.py", "flaky")
        self.ledger.ingest_audit(
            _audit("d1", findings=[_finding(*self.KEY), _finding(*other_key)])
        )
        self.ledger.record_fix_claimed(self.KEY)
        self.ledger.ingest_audit(
            _audit(
                "d2", verdicts=[_observed_fixed(self.KEY)],
                capture_coverage={"cell-1": "ok"},
            )
        )
        self.ledger.record_base_rebase()
        lineage = self.ledger.key_lineage()
        self.assertIn(
            RECORD_KIND_FINDING_REOPENED,
            [record["type"] for record in lineage[self.KEY]],
        )
        self.assertNotIn(
            RECORD_KIND_FINDING_REOPENED,
            [record["type"] for record in lineage[other_key]],
        )


if __name__ == "__main__":
    unittest.main()
