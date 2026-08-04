"""Review/fix ledger and anti-divergence policy tests."""

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


def result(attempt_id, schema, *, findings=(), details=None):
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


class ReviewFixLoopTests(unittest.TestCase):
    def run_loop(self, factory, *, policy=ReviewFixPolicy(), paths=("feature.txt",)):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        run_dir = Path(temporary.name) / "run"
        audit = AuditJournal(
            run_dir,
            "review-test",
            actor=AuditActor("kernel", "controller"),
            evidence_classification="component",
        )
        evidence = EvidenceCatalog(audit=audit)
        loop = ReviewFixLoop(
            run_id="review-test",
            objective="Make the feature correct.",
            acceptance_criteria=(
                {"id": "correct", "statement": "Feature is correct."},
            ),
            allowed_paths=("feature.txt",),
            changed_paths=paths,
            executor_factory=factory,
            evidence=evidence,
            audit=audit,
            policy=policy,
        )
        return loop.run(), audit, evidence

    def test_fix_is_verified_and_regression_review_closes_ledger_entry(self):
        finding = {
            "id": "wrong",
            "statement": "The value is reversed.",
            "category": "correctness",
            "severity": "major",
            "requires_disposition": True,
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
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                    ),
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [key]},
                    )
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [key]},
                    )
                ],
            }
        )

        outcome, audit, evidence = self.run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.cycles, 2)
        self.assertEqual(
            [call[0] for call in factory.calls],
            [
                "review",
                "fix",
                "verify",
                "review",
            ],
        )
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        self.assertEqual(ledger["findings"][key]["cycles_seen"], [1])
        self.assertIn("Attack what", factory.calls[-1][1]["regression_focus"])
        audit.finalize("succeeded", result=outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

    def test_duplicate_and_surface_growing_finding_never_reaches_fixer(self):
        finding = {
            "id": "growth-a",
            "statement": "Add every possible token.",
            "category": "completeness",
            "severity": "critical",
            "requires_disposition": False,
            "file": "feature.txt",
            "subject": "expand token table",
            "score": 95,
            "fix_cost": "surface-growing",
            "protects": "reviewer preference",
            "scope_expanding": True,
        }
        duplicate = {**finding, "id": "growth-b"}
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding, duplicate),
                    )
                ],
            }
        )

        outcome, _, evidence = self.run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual([call[0] for call in factory.calls], ["review"])
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        record = ledger["findings"]["feature.txt:expand-token-table"]
        self.assertEqual(record["outcome"], "scope_screened")
        self.assertEqual(record["occurrences"], 2)
        self.assertEqual(ledger["cycles"][0]["within_cycle_duplicates"], 1)

    def test_cycle_limit_blocks_required_finding_instead_of_hiding_it_as_debt(self):
        finding = {
            "id": "contract",
            "statement": "The output violates the contract.",
            "category": "correctness",
            "severity": "critical",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "contract mismatch",
            "score": 60,
            "fix_cost": "local",
            "protects": "",
            "contract_violation": False,
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    )
                ],
            }
        )
        policy = ReviewFixPolicy(
            mechanical_cycle_limit=1,
            technical_debt_sink_enabled=True,
        )

        outcome, audit, evidence = self.run_loop(factory, policy=policy)

        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason, "cycle limit reached")
        self.assertEqual(outcome.open_finding_keys, ("feature.txt:contract-mismatch",))
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(
            ledger["findings"]["feature.txt:contract-mismatch"]["outcome"],
            "open",
        )
        audit.finalize("blocked", result=outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

    def test_guards_and_followup_stages_can_be_independently_disabled(self):
        finding = {
            "id": "optional-growth",
            "statement": "Grow the table.",
            "category": "completeness",
            "severity": "major",
            "requires_disposition": False,
            "file": "feature.txt",
            "subject": "grow table",
            "score": 90,
            "fix_cost": "surface-growing",
            "protects": "",
        }
        key = "feature.txt:grow-table"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    )
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [key]},
                    )
                ],
            }
        )
        policy = ReviewFixPolicy(
            citation_guard_enabled=False,
            scope_expansion_guard_enabled=False,
            targeted_verification_enabled=False,
            regression_review_enabled=False,
        )

        outcome, _, evidence = self.run_loop(factory, policy=policy)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual([call[0] for call in factory.calls], ["review", "fix"])
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        self.assertFalse(ledger["policy"]["citation_guard_enabled"])
        self.assertFalse(ledger["policy"]["scope_expansion_guard_enabled"])
        self.assertFalse(ledger["policy"]["targeted_verification_enabled"])
        self.assertFalse(ledger["policy"]["regression_review_enabled"])

    def test_closed_finding_is_not_readjudicated_without_new_evidence(self):
        def finding(identifier, subject):
            return {
                "id": identifier,
                "statement": f"{subject} is wrong.",
                "category": "correctness",
                "severity": "major",
                "requires_disposition": True,
                "file": "feature.txt",
                "subject": subject,
                "score": 90,
                "fix_cost": "local",
                "protects": "acceptance criterion correct",
            }

        a = finding("a", "first issue")
        b = finding("b", "second issue")
        a_key = "feature.txt:first-issue"
        b_key = "feature.txt:second-issue"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(a, b),
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(b,),
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(a,),
                    ),
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [a_key, b_key]},
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [b_key]},
                    ),
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [a_key, b_key]},
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [b_key]},
                    ),
                ],
            }
        )

        outcome, _, evidence = self.run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][a_key]["outcome"], "fixed")
        self.assertEqual(ledger["findings"][b_key]["outcome"], "fixed")
        self.assertEqual(ledger["cycles"][2]["ledger_collapses"], 1)
        self.assertEqual(
            [call[0] for call in factory.calls],
            ["review", "fix", "verify", "review", "fix", "verify", "review"],
        )


if __name__ == "__main__":
    unittest.main()
