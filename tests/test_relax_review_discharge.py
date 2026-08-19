"""CB3-06 finding-red proof: file-anchor screening closes the review deadlock.

Specimen: CB2-02 attempt-1 — a review finding anchored (via ``file``) outside
the node's writable paths, with ``required_paths`` empty. At the frozen base
the only screening predicate inspects ``required_paths``, so this finding is
never screened and recurs every cycle until the cycle ceiling blocks an
otherwise gate-passing candidate. On the candidate the finding is screened at
ingest, journaled with its full payload, excluded from
``fix_keys``/``open_required()``, and still eligible for cross-node transfer.

Amended for the scope-screen false-positive repair. CB3-06 applied that
remedy to *every* out-of-grant anchor, including one marked
``contract_violation``/``requires_disposition`` — and ``scope_screened`` is
outside ``open_required()``'s set, so those findings were not deferred but
discharged: the node passed its gate with a critical violation erased and no
counter anywhere. The anchor screen now carries the same
``contract_violation``/``requires_disposition`` exemption the
``scope_expanding`` screen four lines below it has always carried, so a
required finding stays open and the node stops loudly. The ceiling remedy is
unchanged for the findings the screen still owns — everything not required —
and both shapes are covered below.

Whether a genuinely out-of-grant *required* finding should block here, be
carried to a successor, or be routed to a collector is a live design question
elsewhere; this file asserts only that it is not silently discharged.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


class _Factory:
    def __init__(self, scripts):
        self.scripts = {name: list(values) for name, values in scripts.items()}
        self.calls = []

    def __call__(self, stage, attempt):
        self.calls.append((stage, json.loads(attempt.context)))
        script = self.scripts[stage].pop(0)
        return _ScriptedExecutor(script)


class ReviewDischargeScreeningTests(unittest.TestCase):
    """Red construction lives entirely inside setUp/test methods (program rule 6)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name) / "run"

    def _run_loop(self, factory, *, policy=None, **loop_options):
        audit = AuditJournal(
            self.run_dir,
            "review-discharge-test",
            actor=AuditActor("kernel", "controller"),
            evidence_classification="component",
        )
        evidence = EvidenceCatalog(audit=audit)
        loop = ReviewFixLoop(
            run_id="review-discharge-test",
            objective="Make the feature correct.",
            acceptance_criteria=(
                {"id": "correct", "statement": "Feature is correct."},
            ),
            allowed_paths=("feature.txt",),
            changed_paths=("feature.txt",),
            executor_factory=factory,
            evidence=evidence,
            audit=audit,
            policy=policy or ReviewFixPolicy(),
            **loop_options,
        )
        return loop.run(), audit, evidence

    def _out_of_grant_finding(self):
        # CB2-02 attempt-1 shape: file anchor outside the node's writable
        # paths, required_paths empty, and a flag that (at the frozen base)
        # escapes the only existing screening predicate.
        return {
            "id": "plan-anchor",
            "statement": "The plan document needs a new acceptance criterion.",
            "category": "process",
            "severity": "critical",
            "file": "plan.md",
            "subject": "plan needs criterion",
            "score": 90,
            "fix_cost": "structural",
            "protects": "acceptance criterion correct",
            "contract_violation": True,
            "requires_disposition": True,
        }

    def test_out_of_grant_required_finding_is_never_discharged_in_silence(self):
        finding = self._out_of_grant_finding()
        key = "plan.md:plan-needs-criterion"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                ],
                "fix": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [key]},
                    )
                ],
                "verify": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": []},
                    )
                ],
            }
        )
        policy = ReviewFixPolicy(
            mechanical_cycle_limit=2,
            no_progress_stop_enabled=False,
        )

        outcome, audit, evidence = self._run_loop(factory, policy=policy)

        # CB3-06 read "must not deadlock" as "may be discharged", and this
        # finding is a critical contract violation. The verdict it produced --
        # succeeded, no open keys, nothing counted anywhere -- is a green run
        # over lost work, so the node now stops on it instead.
        self.assertEqual(outcome.status, "blocked", outcome.reason)
        self.assertIn(key, outcome.open_finding_keys)
        self.assertEqual([item["key"] for item in outcome.open_findings], [key])
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        record = ledger["findings"][key]
        self.assertNotEqual(record["outcome"], "scope_screened")
        self.assertTrue(record["anchor_out_of_grant"])
        # The full finding payload survives in the journal.
        self.assertEqual(record["file"], "plan.md")
        self.assertEqual(record["statement"], finding["statement"])
        self.assertTrue(record["contract_violation"])
        self.assertTrue(record["requires_disposition"])
        self.assertEqual(ledger["scope_screening"]["screened_count"], 0)
        audit.finalize("blocked", result=outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

    def test_out_of_grant_optional_finding_still_spares_the_cycle_ceiling(self):
        """CB3-06's own remedy, on the findings the screen still owns.

        The same specimen with the two escalation flags cleared: the node
        cannot write ``plan.md`` and nothing obliges it to, so the finding is
        screened after a single review and the ceiling is never burned. What
        has changed since CB3-06 is only that the screen is now countable.
        """

        finding = {
            **self._out_of_grant_finding(),
            "contract_violation": False,
            "requires_disposition": False,
        }
        key = "plan.md:plan-needs-criterion"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                ],
            }
        )

        outcome, _, evidence = self._run_loop(factory)

        self.assertEqual([call[0] for call in factory.calls], ["review"])
        self.assertEqual(outcome.cycles, 1)
        self.assertEqual(outcome.status, "succeeded", outcome.reason)
        self.assertNotIn(key, outcome.open_finding_keys)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "scope_screened")
        self.assertEqual(
            ledger["scope_screening"],
            {
                "screened_count": 1,
                "screened_finding_keys": [key],
                "by_class": {"anchor_out_of_grant": 1},
                "required_finding_keys": [],
            },
        )
        self.assertEqual(dict(outcome.scope_screening), ledger["scope_screening"])

    def test_out_of_grant_finding_still_transfers_to_a_resolvable_downstream_owner(
        self,
    ):
        finding = self._out_of_grant_finding()
        key = "plan.md:plan-needs-criterion"
        # A full review/fix/verify script lets the loop reach a terminal
        # status either way: on the candidate the finding is transferred
        # after the first review and the rest of the script is unused; at
        # the frozen base (no anchor screening) it instead runs the ordinary
        # fix/verify/re-review cycle to a generic "fixed" close, proving the
        # transfer mechanism was never engaged for the out-of-grant anchor.
        factory = _Factory(
            {
                "review": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                    ),
                ],
                "fix": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [key]},
                    )
                ],
                "verify": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [key]},
                    )
                ],
            }
        )

        outcome, _, _ = self._run_loop(
            factory,
            finding_transfer_targets={"plan.md": "DOCS"},
            origin_node_id="A",
        )

        self.assertEqual(outcome.status, "succeeded", outcome.reason)
        self.assertEqual(len(outcome.transferred_findings), 1)
        transfer = outcome.transferred_findings[0]
        self.assertEqual(transfer["key"], key)
        self.assertEqual(transfer["transferred_to"], "DOCS")
        self.assertEqual(transfer["origin_node"], "A")
        self.assertEqual([call[0] for call in factory.calls], ["review"])

    def test_in_grant_finding_flow_is_unchanged(self):
        finding = {
            "id": "wrong",
            "statement": "The value is reversed.",
            "category": "correctness",
            "severity": "major",
            "requires_disposition": True,
            "contract_violation": True,
            "file": "feature.txt",
            "subject": "wrong value",
            "score": 90,
            "fix_cost": "local",
            "protects": "acceptance criterion correct",
        }
        key = "feature.txt:wrong-value"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                    ),
                ],
                "fix": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [key]},
                    )
                ],
                "verify": [
                    lambda attempt: _result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [key]},
                    )
                ],
            }
        )

        outcome, _, evidence = self._run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.cycles, 2)
        self.assertEqual(
            [call[0] for call in factory.calls],
            ["review", "fix", "verify", "review"],
        )
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")


if __name__ == "__main__":
    unittest.main()
