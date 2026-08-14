"""CB3-06 finding-red proof: file-anchor screening closes the review deadlock.

Specimen: CB2-02 attempt-1 — a review finding anchored (via ``file``) outside
the node's writable paths, with ``required_paths`` empty, marked
``contract_violation``/``requires_disposition``. At the frozen base the only
screening predicate inspects ``required_paths``, so this finding is never
screened; the ``contract_violation``/``requires_disposition`` escape then
keeps it ``open`` forever and it recurs every cycle until the cycle ceiling
blocks an otherwise gate-passing candidate. On the candidate the finding is
screened at ingest, journaled with its full payload, excluded from
``fix_keys``/``open_required()``, and still eligible for cross-node transfer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs.attempts import TaskResult
from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_results import semantic_payload
from harness_labs.review_fix import ReviewFixLoop, ReviewFixPolicy


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

    def test_out_of_grant_file_anchor_finding_is_screened_not_the_cycle_ceiling(self):
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

        # An obligation unfixable by contract (its anchor sits outside the
        # node's grant) must not deadlock a gate-passing candidate behind the
        # cycle ceiling.
        self.assertEqual(outcome.status, "succeeded", outcome.reason)
        self.assertNotIn(key, outcome.open_finding_keys)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        record = ledger["findings"][key]
        self.assertEqual(record["outcome"], "scope_screened")
        # The full finding payload survives screening in the journal.
        self.assertEqual(record["file"], "plan.md")
        self.assertEqual(record["statement"], finding["statement"])
        self.assertTrue(record["contract_violation"])
        self.assertTrue(record["requires_disposition"])
        audit.finalize("succeeded", result=outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

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
