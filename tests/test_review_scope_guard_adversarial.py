"""Adversarial tests for the review loop's scope-screening guard.

Why this file exists
--------------------
``ReviewLedger.ingest`` screens a finding out of the loop by stamping it
``scope_screened``.  That outcome is *not* in ``open_required``'s or
``open_all``'s ``{"open", "pending_review"}`` set, so a screened finding
stops being an obligation: it does not block the node, it produces no
counter and no escalation field, and the loop can exit ``succeeded`` with it
silently discharged.  The dangerous direction for this guard is therefore
not "it blocks too much" but "a real, in-grant, fixable finding is
discharged and nobody is ever told".  Every test here attacks that
direction.

Convention for expected failures
--------------------------------
Tests that encode an invariant the code does **not** currently satisfy are
named ``test_xfail_*`` and carry ``@unittest.expectedFailure``.  Both the
prefix and the decorator are greppable, and the suite stays green while the
expected-failure count stays visible.  When a fix lands, the decorator turns
the test into an *unexpected success*, which pytest reports -- so a fix gets
a loud signal rather than a silent pass.  Tests without the prefix assert
behaviour the guard already gets right; they are regression fences against a
fix that over-corrects into a write-outside-scope bug.

Each docstring states the invariant and what a violation costs.
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
from harness_labs.featurerun.review_fix import ReviewFixLoop, ReviewFixPolicy


def _result(attempt_id, schema, *, findings=(), details=None):
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


class _ScriptedExecutor:
    def __init__(self, build_result):
        self.build_result = build_result

    def execute(self, attempt):
        return self.build_result(attempt)


class _CooperativeFactory:
    """A reviewer/fixer pair that never itself loses a finding.

    The reviewer reports the findings scripted for that cycle; the fixer
    addresses exactly the keys the loop hands it and the verifier verifies
    exactly what the fixer addressed.  Anything that goes missing between the
    reviewer and the ledger was therefore lost by the loop, not by the
    workers -- which is the only thing these tests are trying to measure.
    """

    def __init__(self, reviews: Mapping[int, tuple[Mapping[str, Any], ...]]):
        self.reviews = dict(reviews)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def fix_keys_seen(self) -> set[str]:
        return {
            key
            for stage, context in self.calls
            if stage == "fix"
            for key in context["fix_finding_keys"]
        }

    def __call__(self, stage, attempt):
        context = json.loads(attempt.context)
        self.calls.append((stage, context))
        if stage == "review":
            findings = self.reviews.get(context["cycle"], ())
            return _ScriptedExecutor(
                lambda a: _result(
                    a.attempt_id, "review-fix-review/1", findings=findings
                )
            )
        keys = list(context["fix_finding_keys"])
        if stage == "fix":
            return _ScriptedExecutor(
                lambda a: _result(
                    a.attempt_id,
                    "review-fix-fix/1",
                    details={"addressed_finding_keys": keys},
                )
            )
        return _ScriptedExecutor(
            lambda a: _result(
                a.attempt_id,
                "review-fix-verify/1",
                details={"verified_finding_keys": keys},
            )
        )


def finding(**overrides: Any) -> dict[str, Any]:
    """A plain, fixable, required correctness finding."""

    base = {
        "id": "defect",
        "statement": "The computed value is reversed.",
        "subject": "reversed value",
        "category": "correctness",
        "severity": "major",
        "score": 90,
        "fix_cost": "local",
        "protects": "acceptance criterion correct",
        "requires_disposition": True,
        "contract_violation": False,
        "scope_expanding": False,
        "file": "feature.txt",
    }
    base.update(overrides)
    return base


def inherited_record(**overrides: Any) -> dict[str, Any]:
    """A full ledger record as ``open_records`` exports it for a successor."""

    base = {
        "key": "legacy.py:reversed-value",
        "file": "legacy.py",
        "subject": "reversed value",
        "statement": "The computed value is reversed.",
        "category": "correctness",
        "severity": "major",
        "score": 90,
        "fix_cost": "local",
        "protects": "acceptance criterion correct",
        "requires_disposition": True,
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
    base.update(overrides)
    return base


class ScopeGuardAdversarialTests(unittest.TestCase):
    """Drive the real loop; a mock cannot exhibit a silent-discharge bug."""

    def build_loop(
        self,
        factory,
        *,
        policy=ReviewFixPolicy(),
        paths=("feature.txt",),
        allowed_paths=("feature.txt",),
        audit=None,
        evidence=None,
        **loop_options,
    ):
        if audit is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            audit = AuditJournal(
                Path(temporary.name) / "run",
                "scope-guard-test",
                actor=AuditActor("kernel", "controller"),
                evidence_classification="component",
            )
            evidence = EvidenceCatalog(audit=audit)
        loop = ReviewFixLoop(
            run_id="scope-guard-test",
            objective="Make the feature correct.",
            acceptance_criteria=(
                {"id": "correct", "statement": "Feature is correct."},
            ),
            allowed_paths=allowed_paths,
            changed_paths=paths,
            executor_factory=factory,
            evidence=evidence,
            audit=audit,
            policy=policy,
            **loop_options,
        )
        return loop, audit, evidence

    def run_loop(self, factory, **options):
        loop, audit, evidence = self.build_loop(factory, **options)
        outcome = loop.run()
        return outcome, json.loads(evidence.open(outcome.ledger_ref)), evidence

    def screen_one(self, item, *, allowed_paths, **options):
        """Report one finding in cycle 1; return (outcome, record, factory)."""

        factory = _CooperativeFactory({1: (item,)})
        outcome, ledger, _ = self.run_loop(
            factory, allowed_paths=allowed_paths, **options
        )
        self.assertEqual(len(ledger["findings"]), 1, ledger["findings"])
        record = next(iter(ledger["findings"].values()))
        return outcome, record, factory

    # ------------------------------------------------------------------
    # Known defect 1: the anchor is compared as an opaque string.
    # ------------------------------------------------------------------

    def test_line_suffixed_anchor_is_not_screened(self):
        """A ``path:line`` anchor names a granted file and must not be screened.

        ``schemas/review-ledger.schema.json`` has no ``line`` property, so a
        reviewer that wants to cite a line has nowhere to put it but ``file``.
        ``paths_outside_scope`` compares the raw string, so ``feature.txt:100``
        matches neither ``feature.txt`` nor ``feature.txt/``: the most precise
        reviewers -- the ones that cite a line -- are exactly the ones whose
        findings evaporate.
        """

        _, record, factory = self.screen_one(
            finding(file="feature.txt:100"), allowed_paths=("feature.txt",)
        )
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], factory.fix_keys_seen())

    # ------------------------------------------------------------------
    # Known defect 2: branch order robs the later exemption of its findings.
    # ------------------------------------------------------------------

    def test_contract_violation_survives_an_out_of_grant_anchor(self):
        """A contract violation must never be discharged by the anchor screen.

        The ``scope_expanding`` screen four lines below deliberately exempts
        ``contract_violation`` and ``requires_disposition`` -- those must be
        escalated, never dropped.  The anchor screen runs first and carries no
        such exemption, so it consumes the very findings the exemption exists
        to protect.  The finding should stay open (blocking the node) or be
        transferred; it must not end as ``scope_screened``.
        """

        _, record, _ = self.screen_one(
            finding(
                file="contract/api.py",
                severity="critical",
                score=95,
                contract_violation=True,
                requires_disposition=False,
            ),
            allowed_paths=("feature.txt",),
        )
        self.assertNotEqual(record["outcome"], "scope_screened")

    def test_succeeded_run_never_hides_a_screened_required_finding(self):
        """A green run is a claim that nothing was left undischarged.

        This is the most damaging shape of the bug: the loop exits
        ``succeeded`` / ``cleared`` with empty ``open_finding_keys`` while a
        ``requires_disposition`` finding sits in the ledger stamped
        ``scope_screened``.  Nothing downstream reads that stamp, so the run
        reads as converged.  Invariant: if any finding carrying
        ``requires_disposition`` or ``contract_violation`` was screened, the
        loop may not report ``succeeded`` with no open keys.
        """

        outcome, record, _ = self.screen_one(
            finding(file="other/module.py", requires_disposition=True),
            allowed_paths=("feature.txt",),
        )
        screened_required = record["outcome"] == "scope_screened" and (
            record["requires_disposition"] or record["contract_violation"]
        )
        self.assertFalse(
            screened_required
            and outcome.status == "succeeded"
            and not outcome.open_finding_keys,
            "loop reported a clean convergence while discharging a required "
            f"finding: {record['key']}",
        )

    # ------------------------------------------------------------------
    # The family: anchors that name a granted path in a different spelling.
    # ------------------------------------------------------------------

    def test_equivalent_anchor_spellings_are_not_screened(self):
        """Path-equivalent anchor spellings must resolve to the same grant.

        ``normalize_allowed_paths`` normalizes the *grant* side (``./``
        prefixes, trailing slashes and duplicate separators all collapse) but
        the anchor side is compared raw.  ``./feature.txt`` and ``src//app.py``
        denote granted files by every filesystem and by ``PurePosixPath``; the
        guard alone disagrees, and each disagreement discharges a finding.
        """

        screened = []
        for spelling in ("./feature.txt", "src//app.py", "./src/app.py"):
            _, record, _ = self.screen_one(
                finding(file=spelling, subject=f"defect in {spelling}"),
                allowed_paths=("feature.txt", "src/app.py"),
            )
            if record["outcome"] == "scope_screened":
                screened.append(spelling)
        self.assertEqual(screened, [])

    def test_debatable_whitespace_padded_anchor_is_not_screened(self):
        """DEBATABLE INVARIANT -- reported separately, not a confident defect.

        A leading space in ``file`` is a reviewer formatting slip, not a
        path-equivalence question: POSIX allows a filename to begin with a
        space, so stripping is a guess about intent rather than a
        normalization.  It is filed as an expected failure because the cost of
        guessing wrong (screening a real finding) is much higher than the cost
        of accepting a padded anchor -- but a maintainer could reasonably
        reject this invariant, and nothing else in this file depends on it.
        """

        _, record, _ = self.screen_one(
            finding(file=" feature.txt"), allowed_paths=("feature.txt",)
        )
        self.assertNotEqual(record["outcome"], "scope_screened")

    # ------------------------------------------------------------------
    # The family: the anchor is consulted while required_paths is ignored.
    # ------------------------------------------------------------------

    def test_anchor_screen_respects_required_paths_inside_the_grant(self):
        """A finding fixable entirely within the grant must not be screened.

        A reviewer anchors a finding where the defect is *visible*
        (``producer.py``) and declares in ``required_paths`` where it must be
        *fixed* (``feature.txt``, which this node owns).  The loop already
        trusts ``required_paths``: it recomputes ``scope_expanding`` from them
        a few lines earlier, and here they are entirely in grant, so the
        finding is not scope-expanding at all.  The anchor screen discharges it
        anyway, and the transfer machinery cannot rescue it because
        ``transfer_scope_expanding`` falls back to the anchor once the required
        paths turn out to be local.  This loses work the node was both able and
        obliged to do.
        """

        _, record, factory = self.screen_one(
            finding(file="producer.py", required_paths=["feature.txt"]),
            allowed_paths=("feature.txt",),
        )
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], factory.fix_keys_seen())

    # ------------------------------------------------------------------
    # The family: inheritance and continuation carry a stale grant.
    # ------------------------------------------------------------------

    @unittest.expectedFailure
    def test_xfail_inherited_open_finding_is_not_screened_by_successor_grant(self):
        """An inherited obligation must not vanish because the grant narrowed.

        A blocked run exports ``open_records`` so its successor discharges the
        findings instead of rediscovering them.  Those records were minted
        locally, so ``origin_node`` is empty and the ``origin_node`` bypass
        (which protects *transferred* findings whose anchor names the origin's
        path) does not apply.  The successor's own grant then screens them out
        on the first ingest -- before any reviewer has looked at them -- so the
        escalation that blocked the predecessor is erased by a run that never
        considered it.
        """

        record_in = inherited_record()
        factory = _CooperativeFactory({})
        outcome, ledger, _ = self.run_loop(
            factory,
            allowed_paths=("feature.txt",),
            inherited_findings=(record_in,),
        )
        record = ledger["findings"][record_in["key"]]
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertNotEqual(outcome.status, "succeeded")

    def test_continuation_screens_against_the_continuation_grant(self):
        """A continuation must screen against the grant it actually holds.

        ``ReviewLedger`` captures ``allowed_paths`` at construction and a
        continuation reuses the predecessor's ledger object wholesale.  So a
        continuation launched with a *wider* grant still screens against the
        predecessor's narrower one, even for a finding the reviewer re-raises
        with ``new_evidence`` precisely to have it reconsidered.  The loop is
        inconsistent with itself here: it recomputes ``scope_expanding`` from
        ``self.allowed_paths`` (the new grant) in the same cycle in which the
        ledger screens from the old one.  A grant widened specifically to let a
        finding be fixed therefore buys nothing.
        """

        item = finding(file="widened/module.py")
        first_factory = _CooperativeFactory({1: (item,)})
        loop, audit, evidence = self.build_loop(
            first_factory, allowed_paths=("feature.txt",)
        )
        first = loop.run()
        key = next(iter(json.loads(evidence.open(first.ledger_ref))["findings"]))

        policy = ReviewFixPolicy()
        second_factory = _CooperativeFactory({2: ({**item, "new_evidence": True},)})
        continuation, _, _ = self.build_loop(
            second_factory,
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

    # ------------------------------------------------------------------
    # Attacks the guard withstood.  These are fences against an over-broad
    # fix: each must keep passing once the defects above are repaired.
    # ------------------------------------------------------------------

    def test_sibling_prefix_directory_is_still_screened(self):
        """A grant on ``src/app`` must not silently cover ``src/app_helpers``.

        The complement of every test above: the guard must keep screening what
        is genuinely out of grant, or a fix for the false-screen bugs becomes a
        write-outside-scope bug.  ``paths_outside_scope`` requires a full
        segment boundary (``root + "/"``), so the sibling is correctly out.
        """

        _, record, _ = self.screen_one(
            finding(file="src/app_helpers/util.py", requires_disposition=False),
            allowed_paths=("src/app",),
        )
        self.assertEqual(record["outcome"], "scope_screened")

    def test_grant_side_spelling_variants_are_normalized(self):
        """Odd spellings *on the grant side* already resolve correctly.

        ``normalize_allowed_paths`` strips ``./`` and trailing slashes and
        collapses duplicate separators through ``PurePosixPath``, so a grant
        written ``./src//`` still covers ``src/app.py``.  Recorded so a fix
        that reworks normalization cannot regress the half that works.
        """

        _, record, factory = self.screen_one(
            finding(file="src/app.py"), allowed_paths=("./src//",)
        )
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], factory.fix_keys_seen())

    def test_directory_grant_covers_its_own_path_and_its_children(self):
        """A directory grant covers the directory anchor itself and files under it.

        ``a/b`` granted, anchored at ``a/b`` (a directory-level finding) and at
        ``a/b/c.py``: both are inside the grant and must survive.  A guard that
        demanded a file anchor would discharge every module-level finding.
        """

        for anchor in ("a/b", "a/b/c.py"):
            with self.subTest(anchor=anchor):
                _, record, _ = self.screen_one(
                    finding(file=anchor, subject=f"defect at {anchor}"),
                    allowed_paths=("a/b",),
                )
                self.assertNotEqual(record["outcome"], "scope_screened")

    def test_unanchored_finding_is_never_screened(self):
        """A finding with no file anchor must stay an obligation.

        ``bool(record["file"])`` guards the screen, so a repository-wide
        finding (the reviewer legitimately has no single file to name) is not
        screened.  Were it screened, the *broadest* findings -- the ones most
        likely to be architectural -- would be the ones lost.
        """

        _, record, factory = self.screen_one(
            finding(file=""), allowed_paths=("feature.txt",)
        )
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertIn(record["key"], factory.fix_keys_seen())

    def test_malformed_grant_fails_loudly_instead_of_screening_everything(self):
        """An unusable grant must abort the loop, not discharge the ledger.

        ``normalize_allowed_paths`` rejects escaping and absolute paths by
        raising, and the raise propagates out of ``ingest`` to the loop's
        failure handler.  The dangerous alternative -- treating a bad grant as
        an empty grant -- would screen out *every* finding and produce a clean
        exit.  Assert the loud failure, and that nothing was screened.
        """

        factory = _CooperativeFactory({1: (finding(),)})
        loop, _, evidence = self.build_loop(factory, allowed_paths=("../outside",))
        outcome = loop.run()

        self.assertEqual(outcome.status, "failed")
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(
            [
                key
                for key, item in ledger["findings"].items()
                if item["outcome"] == "scope_screened"
            ],
            [],
        )

    def test_out_of_grant_anchor_is_rescued_by_a_bound_transfer_target(self):
        """The escape hatch works: a screened anchor still transfers when bound.

        ``transfer_scope_expanding`` treats ``scope_screened`` as eligible when
        ``anchor_out_of_grant`` is set, so a finding whose anchor belongs to a
        uniquely bound downstream node becomes that node's obligation rather
        than disappearing.  This is the only path by which an out-of-grant
        anchor survives today -- which is exactly why the findings with *no*
        bound owner are the ones that vanish.
        """

        _, record, _ = self.screen_one(
            finding(file="consumer.py"),
            allowed_paths=("feature.txt",),
            finding_transfer_targets={"consumer.py": "node-C"},
            origin_node_id="node-B",
        )
        self.assertEqual(record["outcome"], "transferred")
        self.assertEqual(record["transferred_to"], "node-C")


if __name__ == "__main__":
    unittest.main()
