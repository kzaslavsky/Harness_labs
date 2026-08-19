"""Regression fences for the review loop's scope-screening false positives.

``ReviewLedger.ingest`` stamps a finding ``scope_screened`` when the node
cannot act on it.  That outcome sits outside ``open_all``/``open_required``'s
``{"open", "pending_review"}`` set, so a screened finding blocks nothing and
leaves no obligation: screening the wrong finding does not fail loudly, it
deletes work.  Each test below pins one false positive that used to be
screened, one fence that must keep being screened, or the instrumentation that
makes a screen visible at all.

Scope note.  What should *become* of a finding that is genuinely out of grant
-- block, carry to a successor, route to a periodic collector -- is designed
elsewhere and is deliberately not asserted here.  The only assertion made
about a true positive is that it is counted.

These fences are written to stand on their own: they duplicate no helper with
``tests/test_review_scope_guard_adversarial.py`` so that the two suites can
disagree.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_results import semantic_payload
from harness_labs.featurerun.review_fix import (
    ReviewFixLoop,
    ReviewFixPolicy,
    split_anchor_location,
)


def _stage_result(attempt_id: str, schema: str, *, findings=(), details=None):
    return TaskResult(
        attempt_id,
        "succeeded",
        semantic_payload(
            summary="Stage complete.",
            details_schema=schema,
            details={} if details is None else details,
            findings=tuple(findings),
        ),
    )


class _Executor:
    def __init__(self, build):
        self.build = build

    def execute(self, attempt):
        return self.build(attempt)


class _Reviewer:
    """A reviewer/fixer/verifier trio that never itself drops a finding.

    The fixer addresses exactly the keys it is handed and the verifier
    verifies exactly what the fixer addressed, so anything missing between the
    reviewer and the ledger was lost by the loop under test.
    """

    def __init__(self, per_cycle: Mapping[int, tuple[Mapping[str, Any], ...]]):
        self.per_cycle = dict(per_cycle)
        self.dispatched: list[str] = []

    def __call__(self, stage, attempt):
        context = json.loads(attempt.context)
        if stage == "review":
            reported = self.per_cycle.get(context["cycle"], ())
            return _Executor(
                lambda a: _stage_result(
                    a.attempt_id, "review-fix-review/1", findings=reported
                )
            )
        keys = list(context["fix_finding_keys"])
        if stage == "fix":
            self.dispatched.extend(keys)
            return _Executor(
                lambda a: _stage_result(
                    a.attempt_id,
                    "review-fix-fix/1",
                    details={"addressed_finding_keys": keys},
                )
            )
        return _Executor(
            lambda a: _stage_result(
                a.attempt_id,
                "review-fix-verify/1",
                details={"verified_finding_keys": keys},
            )
        )


def _defect(**fields: Any) -> dict[str, Any]:
    """An optional (non-escalating) finding, so only the path test decides it.

    ``requires_disposition``/``contract_violation`` are exemptions from the
    screen in their own right.  Leaving them off is what makes these tests
    measure the path logic instead of the exemption.
    """

    item = {
        "id": "defect",
        "statement": "The computed total is off by one.",
        "subject": "off by one total",
        "category": "correctness",
        "severity": "major",
        "score": 90,
        "fix_cost": "local",
        "protects": "totals are correct",
        "requires_disposition": False,
        "contract_violation": False,
        "scope_expanding": False,
        "file": "feature.txt",
    }
    item.update(fields)
    return item


class ScopeScreenTestCase(unittest.TestCase):
    def make_loop(
        self,
        reviewer,
        *,
        allowed_paths=("feature.txt",),
        changed_paths=("feature.txt",),
        policy=None,
        audit=None,
        evidence=None,
        **options,
    ):
        if audit is None:
            workspace = tempfile.TemporaryDirectory()
            self.addCleanup(workspace.cleanup)
            audit = AuditJournal(
                Path(workspace.name) / "run",
                "scope-fence",
                actor=AuditActor("kernel", "controller"),
                evidence_classification="component",
            )
            evidence = EvidenceCatalog(audit=audit)
        loop = ReviewFixLoop(
            run_id="scope-fence",
            objective="Keep the totals correct.",
            acceptance_criteria=({"id": "totals", "statement": "Totals correct."},),
            allowed_paths=allowed_paths,
            changed_paths=changed_paths,
            executor_factory=reviewer,
            evidence=evidence,
            audit=audit,
            policy=policy or ReviewFixPolicy(),
            **options,
        )
        return loop, audit, evidence

    def review_once(self, item, **options):
        """Report ``item`` in cycle one; return (outcome, its record, reviewer)."""

        reviewer = _Reviewer({1: (item,)})
        loop, _, evidence = self.make_loop(reviewer, **options)
        outcome = loop.run()
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(len(ledger["findings"]), 1, ledger["findings"])
        return outcome, next(iter(ledger["findings"].values())), reviewer


class AnchorSpellingTests(ScopeScreenTestCase):
    """(1) and (2): the anchor must be read as the path it denotes."""

    def test_line_suffixed_anchor_resolves_to_its_granted_path(self):
        outcome, record, reviewer = self.review_once(
            _defect(file="feature.txt:100"), allowed_paths=("feature.txt",)
        )

        self.assertEqual(record["outcome"], "fixed")
        self.assertIn(record["key"], reviewer.dispatched)
        # ``file`` is half of the finding key, so it is preserved verbatim and
        # the resolved location lives in the new fields beside it.
        self.assertEqual(record["file"], "feature.txt:100")
        self.assertEqual(record["anchor_path"], "feature.txt")
        self.assertEqual(record["line"], 100)
        self.assertIsNone(record["end_line"])
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_line_range_anchor_resolves_to_its_granted_path(self):
        _, record, _ = self.review_once(
            _defect(file="src/app.py:12-40"), allowed_paths=("src",)
        )

        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertEqual(record["anchor_path"], "src/app.py")
        self.assertEqual((record["line"], record["end_line"]), (12, 40))

    def test_structured_line_field_leaves_the_anchor_a_pure_path(self):
        _, record, _ = self.review_once(
            _defect(file="feature.txt", line=7, end_line=9),
            allowed_paths=("feature.txt",),
        )

        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertEqual(record["file"], "feature.txt")
        self.assertEqual((record["line"], record["end_line"]), (7, 9))

    def test_equivalent_anchor_spellings_resolve_to_the_same_grant(self):
        for spelling, grant in (
            ("./feature.txt", "feature.txt"),
            ("src//app.py", "src/app.py"),
            ("./src/app.py", "src/app.py"),
            ("src/app.py/", "src/app.py"),
        ):
            with self.subTest(spelling=spelling):
                _, record, reviewer = self.review_once(
                    _defect(file=spelling, subject=f"defect at {spelling}"),
                    allowed_paths=(grant,),
                )
                self.assertNotEqual(record["outcome"], "scope_screened")
                self.assertIn(record["key"], reviewer.dispatched)

    def test_a_granted_file_named_like_a_location_keeps_its_own_name(self):
        """``notes:10`` is a legal filename, and a grant on it must win.

        The location suffix is a fallback, tried only when the anchor as
        written is not in grant, so recognising the shape can never shorten a
        path that the node was actually granted.
        """

        _, record, _ = self.review_once(
            _defect(file="notes:10"), allowed_paths=("notes:10",)
        )

        self.assertNotEqual(record["outcome"], "scope_screened")

    def test_a_line_column_anchor_is_not_guessed_at(self):
        """Beyond one suffix the shape is guesswork, so it is left alone."""

        self.assertEqual(split_anchor_location("mod.py:12:4"), ("mod.py:12:4", None, None))
        self.assertEqual(split_anchor_location("mod.py:0"), ("mod.py:0", None, None))
        self.assertEqual(split_anchor_location("mod.py:9-4"), ("mod.py:9-4", None, None))
        self.assertEqual(split_anchor_location("mod.py"), ("mod.py", None, None))


class EscalationExemptionTests(ScopeScreenTestCase):
    """(3): the anchor branch must carry the exemption the branch below has."""

    def test_contract_violation_is_not_discharged_by_the_anchor_screen(self):
        """It stays an obligation and is put in front of the fixer.

        Whether an obligation the node ultimately cannot discharge should
        block, transfer, or be collected is decided elsewhere; all that is
        asserted here is that the anchor screen no longer settles the question
        by deleting the finding.
        """

        outcome, record, reviewer = self.review_once(
            _defect(
                file="contract/api.py",
                severity="critical",
                score=95,
                contract_violation=True,
            ),
            allowed_paths=("feature.txt",),
        )

        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], reviewer.dispatched)
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_requires_disposition_is_not_discharged_by_the_anchor_screen(self):
        outcome, record, reviewer = self.review_once(
            _defect(file="contract/api.py", requires_disposition=True),
            allowed_paths=("feature.txt",),
        )

        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], reviewer.dispatched)
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_a_green_run_never_hides_a_screened_required_finding(self):
        """The tripwire, stated as an invariant over any run.

        The damaging shape is a run reporting ``succeeded`` with no open keys
        while a required finding sits screened in its ledger.  Whatever else
        changes, that combination must not be reachable.
        """

        outcome, record, _ = self.review_once(
            _defect(file="other/module.py", requires_disposition=True),
            allowed_paths=("feature.txt",),
        )

        screened_and_required = record["outcome"] == "scope_screened" and (
            record["requires_disposition"] or record["contract_violation"]
        )
        self.assertFalse(
            screened_and_required
            and outcome.status == "succeeded"
            and not outcome.open_finding_keys,
            f"clean convergence reported while discharging {record['key']}",
        )
        self.assertEqual(outcome.scope_screening["required_finding_keys"], [])


class RequiredPathsTests(ScopeScreenTestCase):
    """(4): judge by where the fix goes, not by where the defect was spotted."""

    def test_a_finding_fixable_entirely_in_grant_is_not_screened(self):
        outcome, record, reviewer = self.review_once(
            _defect(file="producer.py", required_paths=["feature.txt"]),
            allowed_paths=("feature.txt",),
        )

        self.assertEqual(record["outcome"], "fixed")
        self.assertIn(record["key"], reviewer.dispatched)
        self.assertFalse(record["anchor_out_of_grant"])
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_required_paths_in_grant_also_defeat_a_bound_transfer_target(self):
        """The node keeps work it can do rather than handing it downstream.

        ``transfer_scope_expanding`` falls back to the anchor when the required
        paths are all local, so an anchor with a bound owner would otherwise
        claim a finding this node was obliged and able to fix.
        """

        outcome, record, reviewer = self.review_once(
            _defect(file="producer.py", required_paths=["feature.txt"]),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"producer.py": "node-C"},
            origin_node_id="node-B",
        )

        self.assertNotEqual(record["outcome"], "transferred")
        self.assertEqual(outcome.transferred_findings, ())
        self.assertIn(record["key"], reviewer.dispatched)

    def test_a_required_path_outside_the_grant_still_screens(self):
        """The fence for the rule above: one path out of grant is enough."""

        _, record, reviewer = self.review_once(
            _defect(
                file="producer.py",
                required_paths=["feature.txt", "elsewhere/other.py"],
            ),
            allowed_paths=("feature.txt",),
        )

        self.assertEqual(record["outcome"], "scope_screened")
        self.assertNotIn(record["key"], reviewer.dispatched)


class InheritedFindingTests(ScopeScreenTestCase):
    """(5a): a record minted against another grant is not this grant's to screen."""

    def _open_record(self, **fields: Any) -> dict[str, Any]:
        record = {
            "key": "legacy.py:off-by-one-total",
            "file": "legacy.py",
            "subject": "off by one total",
            "statement": "The computed total is off by one.",
            "category": "correctness",
            "severity": "major",
            "score": 90,
            "fix_cost": "local",
            "protects": "totals are correct",
            "requires_disposition": False,
            "contract_violation": False,
            "scope_expanding": False,
            "outcome": "open",
            "outcome_reason": "",
            "cycles_seen": [1],
            "occurrences": 1,
            "source_finding_ids": ["defect"],
            "evidence_refs": ["artifact:review"],
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "",
            "transferred_to": "",
            "transfer_eligible": True,
            "required_paths": [],
            "anchor_out_of_grant": False,
        }
        record.update(fields)
        return record

    def test_an_inherited_open_record_survives_the_successors_grant(self):
        """``open_records`` exports ``origin_node`` empty, so the old bypass missed it.

        A blocked predecessor exports its still-open findings so the successor
        discharges them.  Screening them at first ingest erased the very
        escalation that produced the successor.
        """

        inherited = self._open_record()
        reviewer = _Reviewer({})
        loop, _, evidence = self.make_loop(
            reviewer,
            allowed_paths=("feature.txt",),
            inherited_findings=(inherited,),
        )
        outcome = loop.run()

        ledger = json.loads(evidence.open(outcome.ledger_ref))
        record = ledger["findings"][inherited["key"]]
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertTrue(record["inherited"])
        self.assertIn(inherited["key"], reviewer.dispatched)
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_an_inherited_record_with_a_legacy_line_anchor_still_loads(self):
        """A ledger written before ``line`` existed keeps its location on read."""

        inherited = self._open_record(
            key="legacy.py:86:off-by-one-total", file="legacy.py:86"
        )
        reviewer = _Reviewer({})
        loop, _, evidence = self.make_loop(
            reviewer,
            allowed_paths=("feature.txt",),
            inherited_findings=(inherited,),
        )
        outcome = loop.run()

        record = json.loads(evidence.open(outcome.ledger_ref))["findings"][
            inherited["key"]
        ]
        self.assertEqual(record["file"], "legacy.py:86")
        self.assertEqual(record["anchor_path"], "legacy.py")
        self.assertEqual(record["line"], 86)


class ContinuationGrantTests(ScopeScreenTestCase):
    """(5b): a continuation screens against the grant it actually holds."""

    def test_a_widened_grant_reaches_a_finding_screened_by_the_narrow_one(self):
        item = _defect(file="widened/module.py")
        first_reviewer = _Reviewer({1: (item,)})
        loop, audit, evidence = self.make_loop(
            first_reviewer, allowed_paths=("feature.txt",)
        )
        first = loop.run()
        first_ledger = json.loads(evidence.open(first.ledger_ref))
        key = next(iter(first_ledger["findings"]))
        # The predecessor could not write the path, so the screen is correct
        # here -- and, unlike before, it is counted.
        self.assertEqual(first_ledger["findings"][key]["outcome"], "scope_screened")
        self.assertEqual(first.scope_screening["screened_count"], 1)

        policy = ReviewFixPolicy()
        second_reviewer = _Reviewer(
            {first.cycles + 1: ({**item, "new_evidence": True},)}
        )
        continuation, _, _ = self.make_loop(
            second_reviewer,
            policy=policy,
            allowed_paths=("feature.txt", "widened"),
            audit=audit,
            evidence=evidence,
            resumed_ledger=loop.ledger,
            resume_from_cycle=first.cycles,
            additional_cycles=policy.continuation_cycles,
        )
        outcome = continuation.run()

        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertNotEqual(ledger["findings"][key]["outcome"], "scope_screened")
        self.assertIn(key, second_reviewer.dispatched)
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_a_continuation_that_keeps_the_grant_keeps_the_screen(self):
        """The fence: re-raising alone must not buy an out-of-grant finding in."""

        item = _defect(file="widened/module.py")
        loop, audit, evidence = self.make_loop(
            _Reviewer({1: (item,)}), allowed_paths=("feature.txt",)
        )
        first = loop.run()
        key = next(iter(json.loads(evidence.open(first.ledger_ref))["findings"]))

        policy = ReviewFixPolicy()
        continuation, _, _ = self.make_loop(
            _Reviewer({first.cycles + 1: ({**item, "new_evidence": True},)}),
            policy=policy,
            allowed_paths=("feature.txt",),
            audit=audit,
            evidence=evidence,
            resumed_ledger=loop.ledger,
            resume_from_cycle=first.cycles,
            additional_cycles=policy.continuation_cycles,
        )
        outcome = continuation.run()

        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "scope_screened")


class ScreenVisibilityTests(ScopeScreenTestCase):
    """(6): a screen leaves a counter, in the ledger and in the run's evidence."""

    def test_a_screen_is_counted_per_cycle_and_per_ledger(self):
        outcome, record, _ = self.review_once(
            _defect(file="elsewhere/module.py"), allowed_paths=("feature.txt",)
        )

        self.assertEqual(record["outcome"], "scope_screened")
        self.assertEqual(record["scope_screen_class"], "anchor_out_of_grant")
        self.assertEqual(
            dict(outcome.scope_screening),
            {
                "screened_count": 1,
                "screened_finding_keys": [record["key"]],
                "by_class": {"anchor_out_of_grant": 1},
                "required_finding_keys": [],
            },
        )
        self.assertEqual(outcome.as_dict()["scope_screening"]["screened_count"], 1)

    def test_the_screening_branch_is_named_in_the_record(self):
        _, record, _ = self.review_once(
            _defect(file="feature.txt", required_paths=["outside/other.py"]),
            allowed_paths=("feature.txt",),
        )

        self.assertEqual(record["outcome"], "scope_screened")
        self.assertEqual(record["scope_screen_class"], "scope_expanding")

    def test_the_cycle_entry_carries_the_screen_count(self):
        reviewer = _Reviewer({1: (_defect(file="elsewhere/module.py"),)})
        loop, _, evidence = self.make_loop(reviewer, allowed_paths=("feature.txt",))
        outcome = loop.run()

        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["cycles"][0]["scope_screened"], 1)
        self.assertEqual(ledger["scope_screening"]["screened_count"], 1)

    def test_an_unscreened_run_reports_a_zero_count_rather_than_nothing(self):
        outcome, record, _ = self.review_once(
            _defect(file="feature.txt"), allowed_paths=("feature.txt",)
        )

        self.assertEqual(record["outcome"], "fixed")
        self.assertEqual(outcome.scope_screening["screened_count"], 0)
        self.assertEqual(outcome.scope_screening["screened_finding_keys"], [])


class UnchangedBehaviourTests(ScopeScreenTestCase):
    """What a repair of the above must not trade away."""

    def test_a_sibling_prefix_directory_is_still_out_of_grant(self):
        _, record, reviewer = self.review_once(
            _defect(file="src/app_helpers/util.py"), allowed_paths=("src/app",)
        )

        self.assertEqual(record["outcome"], "scope_screened")
        self.assertNotIn(record["key"], reviewer.dispatched)

    def test_a_directory_grant_covers_the_directory_and_its_children(self):
        for anchor in ("a/b", "a/b/c.py"):
            with self.subTest(anchor=anchor):
                _, record, _ = self.review_once(
                    _defect(file=anchor, subject=f"defect at {anchor}"),
                    allowed_paths=("a/b",),
                )
                self.assertNotEqual(record["outcome"], "scope_screened")

    def test_an_unanchored_finding_is_never_screened(self):
        _, record, reviewer = self.review_once(
            _defect(file=""), allowed_paths=("feature.txt",)
        )

        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], reviewer.dispatched)

    def test_a_malformed_grant_fails_the_loop_rather_than_the_ledger(self):
        """An unusable grant must raise, not degrade into an empty grant.

        An empty grant screens every finding and exits clean, which is the
        worst available failure.  The finding used here is exempt from the
        screen, which is precisely why the grant is validated before any
        exemption is consulted.
        """

        reviewer = _Reviewer({1: (_defect(requires_disposition=True),)})
        loop, _, evidence = self.make_loop(reviewer, allowed_paths=("../outside",))
        outcome = loop.run()

        self.assertEqual(outcome.status, "failed")
        self.assertIn("escapes repository", outcome.reason)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["scope_screening"]["screened_count"], 0)

    def test_an_out_of_grant_anchor_still_transfers_to_a_bound_owner(self):
        outcome, record, _ = self.review_once(
            _defect(file="consumer.py"),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"consumer.py": "node-C"},
            origin_node_id="node-B",
        )

        self.assertEqual(record["outcome"], "transferred")
        self.assertEqual(record["transferred_to"], "node-C")
        self.assertEqual(outcome.scope_screening["screened_count"], 0)

    def test_a_line_suffixed_anchor_transfers_by_its_bare_path(self):
        _, record, _ = self.review_once(
            _defect(file="consumer.py:42"),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"consumer.py": "node-C"},
            origin_node_id="node-B",
        )

        self.assertEqual(record["outcome"], "transferred")
        self.assertEqual(record["transferred_to"], "node-C")
        self.assertEqual(record["required_paths"], ["consumer.py"])


class DirectoryTransferGrantTests(ScopeScreenTestCase):
    """A transfer grant naming a directory must route the files beneath it.

    ``_target_for_path`` used to look beneath a grant only when the grant
    string ended in ``/``.  ``normalize_repository_path`` rejects a trailing
    slash outright, so every grant that survives the plan contract is
    slash-free: no directory grant could ever route a file, and the only
    transfers that ever fired were the exact-filename ones every existing
    fence happened to use.  On the 26-node Flow Editor decomposition that left
    51 of 101 grant entries directory-style and unable to receive anything.
    """

    def test_a_directory_grant_routes_a_file_beneath_it(self):
        _, record, _ = self.review_once(
            _defect(file="retinology/web/routes/l2.py"),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"retinology/web/routes": "WP-10"},
            origin_node_id="WP-02",
        )

        self.assertEqual(record["outcome"], "transferred")
        self.assertEqual(record["transferred_to"], "WP-10")
        self.assertEqual(record["required_paths"], ["retinology/web/routes/l2.py"])

    def test_a_directory_grant_does_not_claim_its_sibling_by_prefix(self):
        """The fence: containment is judged at a segment boundary.

        A naive ``startswith`` repair would hand ``src/app_helpers/util.py`` to
        the owner of ``src/app`` -- a node with no write access to it, which
        would then be asked to fix a file it cannot touch.
        """

        _, record, _ = self.review_once(
            _defect(file="src/app_helpers/util.py"),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"src/app": "WP-10"},
            origin_node_id="WP-02",
        )

        self.assertNotEqual(record["outcome"], "transferred")
        self.assertEqual(record["transferred_to"], "")

    def test_the_deepest_enclosing_directory_grant_owns_the_path(self):
        _, record, _ = self.review_once(
            _defect(file="src/app/view.py"),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"src": "WP-09", "src/app": "WP-10"},
            origin_node_id="WP-02",
        )

        self.assertEqual(record["transferred_to"], "WP-10")

    def test_two_equally_deep_grants_leave_the_path_unowned(self):
        """Ambiguity must not be resolved by dictionary order."""

        _, record, _ = self.review_once(
            _defect(file="src/app/view.py", required_paths=["src/app/view.py", "docs/x.md"]),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"src/app": "WP-10", "docs": "WP-11"},
            origin_node_id="WP-02",
        )

        self.assertNotEqual(record["outcome"], "transferred")


if __name__ == "__main__":
    unittest.main()
