"""Review/fix ledger and anti-divergence policy tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_results import semantic_payload
from harness_labs.featurerun.review_fix import (
    REVIEW_FIX_RESULT_PROTOCOL,
    ReviewFixError,
    ReviewFixLoop,
    ReviewFixPolicy,
    ReviewLedger,
)


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
    def run_loop(
        self,
        factory,
        *,
        policy=ReviewFixPolicy(),
        paths=("feature.txt",),
        allowed_paths=("feature.txt",),
        **loop_options,
    ):
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
            allowed_paths=allowed_paths,
            changed_paths=paths,
            executor_factory=factory,
            evidence=evidence,
            audit=audit,
            policy=policy,
            **loop_options,
        )
        return loop.run(), audit, evidence

    def build_loop(
        self,
        factory,
        *,
        policy=ReviewFixPolicy(),
        paths=("feature.txt",),
        allowed_paths=("feature.txt",),
        evidence=None,
        audit=None,
        **loop_options,
    ):
        """Return the loop itself so a test can continue its ledger."""

        if audit is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            audit = AuditJournal(
                Path(temporary.name) / "run",
                "review-test",
                actor=AuditActor("kernel", "controller"),
                evidence_classification="component",
            )
            evidence = EvidenceCatalog(audit=audit)
        return ReviewFixLoop(
            run_id="review-test",
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
        ), audit, evidence

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
        self.assertIn(
            "Do not discover or authorize new work",
            factory.calls[-1][1]["regression_focus"],
        )
        audit.finalize("succeeded", result=outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

    def test_continuation_resumes_the_blocked_ledger_instead_of_restarting(self):
        """A granted continuation keeps finding identity and cycle numbering.

        This is the whole point of the continuation: the predecessor already
        paid for discovery, so the extra cycles go to discharging what it
        found rather than re-reviewing the same worktree from cycle one.
        """

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
        # One cycle only: review finds the item, the limit lands before any fix.
        policy = ReviewFixPolicy(mechanical_cycle_limit=1, continuation_cycles=2)
        blocked_factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    )
                ]
            }
        )
        loop, audit, evidence = self.build_loop(blocked_factory, policy=policy)
        blocked = loop.run()

        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.cycles, 1)
        self.assertEqual(blocked.open_finding_keys, (key,))
        self.assertEqual(
            [item["key"] for item in blocked.open_findings], [key]
        )

        continuation_factory = _Factory(
            {
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
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
                    ),
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
                    ),
                ],
            }
        )
        continuation, _, _ = self.build_loop(
            continuation_factory,
            policy=policy,
            audit=audit,
            evidence=evidence,
            resumed_ledger=loop.ledger,
            resume_from_cycle=blocked.cycles,
            additional_cycles=policy.continuation_cycles,
        )
        outcome = continuation.run()

        self.assertEqual(outcome.status, "succeeded")
        # Cycle numbering continues, so no attempt id collides with the
        # predecessor's and the ledger reads as one history.
        self.assertEqual(outcome.cycles, 3)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        # Identity survived: the finding was seen in the predecessor's cycle,
        # never re-ingested as a new item.
        self.assertEqual(ledger["findings"][key]["cycles_seen"], [1])
        self.assertEqual([entry["cycle"] for entry in ledger["cycles"]], [1, 2, 3])
        # The continuation opens with a regression review, not a discovery
        # review: it may only confirm the inherited findings, so the cycle it
        # was granted goes to fixing them.
        self.assertEqual(
            [call[0] for call in continuation_factory.calls],
            ["review", "fix", "verify", "review"],
        )
        self.assertIn(
            "Do not discover or authorize new work",
            continuation_factory.calls[0][1]["regression_focus"],
        )
        self.assertEqual(continuation_factory.calls[0][1]["cycle"], 2)
        audit.finalize("succeeded", result=outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

    def test_second_consecutive_continuation_gets_a_real_cycle_budget(self):
        """A continuation chain must keep granting cycles, not stall at two.

        ``cycle`` is cumulative across the chain, so a continuation resumes at
        its predecessor's total. Computing the ceiling as
        ``base + continuation_cycles`` therefore worked only for the *first*
        continuation, where ``resume_from_cycle == base``. The second resumed
        at ``base + granted`` against a limit of ``base + granted``: its first
        review tripped ``cycle >= cycle_limit`` immediately, so it spent a
        review call and blocked without ever reaching a fix. This drives the
        real ledger through two consecutive continuations to pin that.
        """

        def finding(identifier, subject, statement):
            return {
                "id": identifier,
                "statement": statement,
                "category": "correctness",
                "severity": "major",
                "requires_disposition": True,
                "file": "feature.txt",
                "subject": subject,
                "score": 90,
                "fix_cost": "local",
                "protects": "acceptance criterion correct",
            }

        first_key = "feature.txt:first-defect"
        second_key = "feature.txt:second-defect"
        # Futility stops are disabled so the only thing that can end a loop
        # here is the cycle budget -- which is what is under test.
        policy = ReviewFixPolicy(
            mechanical_cycle_limit=1,
            continuation_cycles=2,
            marginal_yield_stop_enabled=False,
            no_progress_stop_enabled=False,
        )

        discovery = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(
                            finding("first", "first defect", "The first is wrong."),
                            finding("second", "second defect", "The second is wrong."),
                        ),
                    )
                ]
            }
        )
        loop, audit, evidence = self.build_loop(discovery, policy=policy)
        blocked = loop.run()
        self.assertEqual((blocked.status, blocked.stop_reason), ("blocked", "cycle_limit"))
        self.assertEqual(blocked.cycles, 1)
        self.assertEqual(set(blocked.open_finding_keys), {first_key, second_key})

        def discharging_factory(key, reviews):
            return _Factory(
                {
                    "review": reviews,
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

        # First continuation: cycles 2 and 3. It discharges one finding and
        # runs out of budget with the other still open -- so it blocks on
        # cycle_limit again, which is the only stop reason a further
        # continuation is granted for.
        first_factory = discharging_factory(
            first_key,
            [
                lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
                lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
            ],
        )
        first_continuation, _, _ = self.build_loop(
            first_factory, policy=policy, audit=audit, evidence=evidence,
            resumed_ledger=loop.ledger,
            resume_from_cycle=blocked.cycles,
            additional_cycles=policy.continuation_cycles,
        )
        self.assertEqual(first_continuation.cycle_budget("mechanical"), 3)
        first_outcome = first_continuation.run()
        self.assertEqual(
            (first_outcome.status, first_outcome.stop_reason),
            ("blocked", "cycle_limit"),
        )
        self.assertEqual(first_outcome.cycles, 3)
        self.assertEqual(first_outcome.open_finding_keys, (second_key,))

        # Second continuation: resumes at cycle 3, so its ceiling must be 5.
        # Under the old arithmetic it was base + granted = 3, i.e. already
        # behind where this loop starts.
        second_factory = discharging_factory(
            second_key,
            [
                lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
                lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
            ],
        )
        second_continuation, _, _ = self.build_loop(
            second_factory, policy=policy, audit=audit, evidence=evidence,
            resumed_ledger=first_continuation.ledger,
            resume_from_cycle=first_outcome.cycles,
            additional_cycles=policy.continuation_cycles,
        )
        budget = second_continuation.cycle_budget("mechanical")
        self.assertEqual(budget, 5)
        self.assertGreater(
            budget,
            policy.mechanical_cycle_limit + policy.continuation_cycles,
            "a second continuation must not be capped at the first one's ceiling",
        )

        second_outcome = second_continuation.run()
        self.assertEqual(second_outcome.status, "succeeded")
        self.assertEqual(second_outcome.cycles, 5)
        # The proof it was not a one-review stall: it reached fix and verify.
        self.assertEqual(
            [call[0] for call in second_factory.calls],
            ["review", "fix", "verify", "review"],
        )
        ledger = json.loads(evidence.open(second_outcome.ledger_ref))
        self.assertEqual(ledger["findings"][first_key]["outcome"], "fixed")
        self.assertEqual(ledger["findings"][second_key]["outcome"], "fixed")
        # One continuous history across all three loops.
        self.assertEqual(
            [entry["cycle"] for entry in ledger["cycles"]], [1, 2, 3, 4, 5]
        )
        audit.finalize("succeeded", result=second_outcome.as_dict())
        AuditJournal.verify(audit.run_dir)

    def test_continuation_rejects_an_incoherent_grant(self):
        factory = _Factory({})
        with self.assertRaises(ValueError):
            self.build_loop(factory, resume_from_cycle=2)
        ledger_owner, _, _ = self.build_loop(factory)
        with self.assertRaises(ValueError):
            self.build_loop(
                factory,
                resumed_ledger=ReviewLedger(ReviewFixPolicy(), "mechanical"),
                resume_from_cycle=1,
                additional_cycles=0,
            )
        with self.assertRaises(ValueError):
            # Re-seeding a ledger that already carries the obligation would
            # collide on its key.
            self.build_loop(
                factory,
                resumed_ledger=ReviewLedger(ReviewFixPolicy(), "mechanical"),
                resume_from_cycle=1,
                additional_cycles=1,
                inherited_findings=({"key": "a.py:thing"},),
            )
        del ledger_owner

    def test_no_change_fix_triggers_one_fresh_recovery_attempt(self):
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
                    lambda attempt: TaskResult(
                        attempt.attempt_id,
                        "failed",
                        {
                            "error": (
                                "writable worker completed without changing "
                                "the repository"
                            ),
                            "error_type": "LiveExecutionError",
                        },
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [key]},
                    ),
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

        outcome, audit, _ = self.run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(
            [call[0] for call in factory.calls],
            ["review", "fix", "fix", "verify", "review"],
        )
        recovery = factory.calls[2][1]["recovery"]
        self.assertEqual(recovery["attempt"], 1)
        self.assertIn("changed implementation method", recovery["instruction"])
        events = [
            json.loads(line)
            for line in (audit.run_dir / "events.jsonl").read_text().splitlines()
        ]
        triggered = [
            event
            for event in events
            if event["event_type"] == "review_fix_recovery_triggered"
        ]
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["status"], "recovering")

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

    def test_scope_expanding_finding_transfers_to_bound_downstream_owner(self):
        finding = {
            "id": "consumer",
            "statement": "Wire the producer into the consumer.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "producer.py",
            "subject": "consumer integration",
            "score": 90,
            "fix_cost": "surface-growing",
            "protects": "AC integration",
            "scope_expanding": False,
            "required_paths": ["consumer.py"],
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    )
                ]
            }
        )

        outcome, _, _ = self.run_loop(
            factory,
            finding_transfer_targets={"consumer.py": "B"},
            origin_node_id="A",
        )

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(outcome.transferred_findings), 1)
        transfer = outcome.transferred_findings[0]
        self.assertEqual(transfer["key"], "producer.py:consumer-integration")
        self.assertEqual(transfer["origin_node"], "A")
        self.assertEqual(transfer["transferred_to"], "B")
        self.assertEqual([call[0] for call in factory.calls], ["review"])

    def test_retained_transfer_survives_replacement_review_without_reopening(self):
        key = "producer.py:consumer-integration"
        transfer = {
            "id": "consumer",
            "key": key,
            "file": "producer.py",
            "subject": "consumer integration",
            "statement": "Wire the producer into the consumer.",
            "category": "integration",
            "severity": "major",
            "score": 90,
            "fix_cost": "surface-growing",
            "protects": "AC integration",
            "requires_disposition": True,
            "contract_violation": False,
            "scope_expanding": True,
            "outcome": "transferred",
            "outcome_reason": "transferred to downstream owner B",
            "cycles_seen": [1],
            "occurrences": 1,
            "source_finding_ids": ["consumer"],
            "evidence_refs": ["artifact:producer"],
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "A",
            "transferred_to": "B",
            "transfer_eligible": True,
            "required_paths": ["consumer.py"],
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(transfer,),
                    )
                ]
            }
        )

        outcome, _, evidence = self.run_loop(
            factory,
            retained_transfers=(transfer,),
            finding_transfer_targets={"consumer.py": "B"},
            origin_node_id="A",
        )

        self.assertEqual(outcome.status, "succeeded", outcome.reason)
        self.assertEqual(len(outcome.transferred_findings), 1)
        self.assertEqual(outcome.transferred_findings[0]["key"], key)
        self.assertEqual(outcome.transferred_findings[0]["transferred_to"], "B")
        self.assertEqual([call[0] for call in factory.calls], ["review"])
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "transferred")
        self.assertEqual(ledger["cycles"][0]["ledger_collapses"], 1)

    def test_mixed_ownership_finding_transfers_only_downstream_paths(self):
        finding = {
            "id": "coupled-consumer",
            "statement": "Finish the consumer cutover after the producer gate.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "producer.py",
            "subject": "coupled consumer integration",
            "score": 90,
            "fix_cost": "structural",
            "protects": "AC integration",
            "required_paths": ["feature.txt", "consumer.py"],
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    )
                ]
            }
        )

        outcome, _, _ = self.run_loop(
            factory,
            finding_transfer_targets={"consumer.py": "B"},
            origin_node_id="A",
        )

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(outcome.transferred_findings), 1)
        transfer = outcome.transferred_findings[0]
        self.assertEqual(transfer["transferred_to"], "B")
        self.assertEqual(transfer["required_paths"], ["consumer.py"])
        self.assertEqual([call[0] for call in factory.calls], ["review"])

    def test_inherited_transfer_is_fixed_by_destination_not_retransferred(self):
        key = "consumer.py:consumer-integration"
        inherited = {
            "key": key,
            "file": "consumer.py",
            "subject": "consumer integration",
            "statement": "Wire the producer into the consumer.",
            "category": "integration",
            "severity": "major",
            "score": 90,
            "fix_cost": "surface-growing",
            "protects": "AC integration",
            "requires_disposition": True,
            "contract_violation": False,
            "scope_expanding": True,
            "outcome": "transferred",
            "outcome_reason": "transferred to downstream owner B",
            "cycles_seen": [1],
            "occurrences": 1,
            "source_finding_ids": ["consumer"],
            "evidence_refs": ["artifact:producer"],
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "A",
            "transferred_to": "B",
            "transfer_eligible": True,
            "required_paths": ["consumer.py"],
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
                    ),
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
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

        outcome, _, evidence = self.run_loop(
            factory,
            allowed_paths=("consumer.py",),
            inherited_findings=(inherited,),
            finding_transfer_targets={"consumer.py": "C"},
            origin_node_id="B",
        )

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.transferred_findings, ())
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        self.assertEqual(
            [call[0] for call in factory.calls],
            ["review", "fix", "verify", "review"],
        )

    def test_inherited_transfer_with_origin_file_anchor_is_not_screened(self):
        # The transfer stamp leaves "file" at the origin's anchor
        # (producer.py) while "required_paths" carries the downstream
        # owner's paths (consumer.py, asserted at line 302 above). The
        # destination's grant covers required_paths but not the origin
        # file anchor; the anchor screen must not discharge this inherited
        # obligation on that basis.
        key = "producer.py:consumer-integration"
        inherited = {
            "key": key,
            "file": "producer.py",
            "subject": "consumer integration",
            "statement": "Wire the producer into the consumer.",
            "category": "integration",
            "severity": "major",
            "score": 90,
            "fix_cost": "surface-growing",
            "protects": "AC integration",
            "requires_disposition": True,
            "contract_violation": False,
            "scope_expanding": True,
            "outcome": "transferred",
            "outcome_reason": "transferred to downstream owner B",
            "cycles_seen": [1],
            "occurrences": 1,
            "source_finding_ids": ["consumer"],
            "evidence_refs": ["artifact:producer"],
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "A",
            "transferred_to": "B",
            "transfer_eligible": True,
            "required_paths": ["consumer.py"],
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
                    ),
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
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

        outcome, _, evidence = self.run_loop(
            factory,
            allowed_paths=("consumer.py",),
            inherited_findings=(inherited,),
            finding_transfer_targets={"consumer.py": "C"},
            origin_node_id="B",
        )

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.transferred_findings, ())
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        self.assertFalse(ledger["findings"][key]["anchor_out_of_grant"])
        self.assertEqual(
            [call[0] for call in factory.calls],
            ["review", "fix", "verify", "review"],
        )

    def test_frozen_inherited_ledger_repairs_only_existing_key(self):
        key = "feature.txt:remaining-defect"
        inherited = {
            "key": key,
            "file": "feature.txt",
            "subject": "remaining defect",
            "statement": "The frozen assertion fails.",
            "category": "correctness",
            "severity": "major",
            "score": 90,
            "fix_cost": "local",
            "protects": "acceptance criterion correct",
            "requires_disposition": True,
            "contract_violation": True,
            "scope_expanding": False,
            "outcome": "open",
            "outcome_reason": "",
            "cycles_seen": [1],
            "occurrences": 1,
            "source_finding_ids": ["remaining"],
            "evidence_refs": ["artifact:prior-verifier"],
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "A",
            "transferred_to": "",
            "transfer_eligible": False,
            "required_paths": ["feature.txt"],
        }
        late = {
            "id": "late",
            "statement": "A newly proposed issue.",
            "category": "review",
            "severity": "major",
            "requires_disposition": True,
            "file": "late.txt",
            "subject": "late issue",
            "score": 90,
            "fix_cost": "local",
            "protects": "criterion",
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(late,),
                    ),
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
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

        outcome, _, evidence = self.run_loop(
            factory,
            inherited_findings=(inherited,),
            inherited_ledger_frozen=True,
        )

        self.assertEqual(outcome.status, "succeeded")
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        self.assertEqual(ledger["findings"]["late.txt:late-issue"]["outcome"], "deferred")

    def test_partial_targeted_verification_uses_remaining_cycle_budget(self):
        findings = tuple(
            {
                "id": subject,
                "statement": f"{subject} fails.",
                "category": "correctness",
                "severity": "major",
                "requires_disposition": True,
                "file": "feature.txt",
                "subject": subject,
                "score": 90,
                "fix_cost": "local",
                "protects": "acceptance criterion correct",
            }
            for subject in ("first", "second")
        )
        first, second = "feature.txt:first", "feature.txt:second"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=findings,
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(findings[1],),
                    ),
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1"
                    ),
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [first, second]},
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [second]},
                    ),
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [first]},
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [second]},
                    ),
                ],
            }
        )

        outcome, _, _ = self.run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.cycles, 3)

    def test_later_review_cannot_add_findings_to_frozen_ledger(self):
        first = {
            "id": "first",
            "statement": "The original value is wrong.",
            "category": "correctness",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "original value",
            "score": 90,
            "fix_cost": "local",
            "protects": "acceptance criterion correct",
        }
        late = {
            "id": "late",
            "statement": "A later reviewer prefers another redesign.",
            "category": "review",
            "severity": "critical",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "later redesign",
            "score": 95,
            "fix_cost": "local",
            "protects": "reviewer preference",
        }
        first_key = "feature.txt:original-value"
        late_key = "feature.txt:later-redesign"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(first,),
                    ),
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(late,),
                    ),
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [first_key]},
                    )
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [first_key]},
                    )
                ],
            }
        )

        outcome, _, evidence = self.run_loop(factory)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual([call[0] for call in factory.calls], [
            "review", "fix", "verify", "review"
        ])
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][first_key]["outcome"], "fixed")
        self.assertEqual(ledger["findings"][late_key]["outcome"], "deferred")
        self.assertEqual(ledger["cycles"][1]["deferred_findings"], 1)

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

    def test_escalated_required_finding_blocks_without_regression_review(self):
        """AC-CC08-2: escalation must still block when regression review is off.

        Without ``regression_review_enabled``, the loop seals right after the
        fix/verify pair (review_fix.py:1043-1058) instead of routing back
        through the "no fix keys left" check at review_fix.py:968-987, so it
        needs its own ``escalated_required`` guard.
        """

        escalated_finding = {
            "id": "cross-node",
            "statement": "Needs another node's file changed.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "cross node fix",
            "score": 90,
            "fix_cost": "structural",
            "protects": "AC integration",
            "required_paths": ["other.py"],
        }
        fixable_finding = {
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
        escalated_key = "feature.txt:cross-node-fix"
        fixable_key = "feature.txt:wrong-value"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(escalated_finding, fixable_finding),
                    )
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={"addressed_finding_keys": [fixable_key]},
                    )
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [fixable_key]},
                    )
                ],
            }
        )
        policy = ReviewFixPolicy(
            escalation_enabled=True, regression_review_enabled=False
        )

        outcome, _, evidence = self.run_loop(factory, policy=policy)

        self.assertEqual(outcome.status, "blocked", outcome.reason)
        self.assertEqual(len(outcome.escalated_findings), 1)
        self.assertEqual(outcome.escalated_findings[0]["key"], escalated_key)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][escalated_key]["outcome"], "escalated")
        self.assertEqual(ledger["findings"][fixable_key]["outcome"], "fixed")

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


    # -- CC-08: escalation primitives and the bounded fix-only loop --------

    def test_escalation_disabled_by_default_adds_only_escalated_findings_key(self):
        """AC-CC08-1: default policy output is unchanged but for one key."""

        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
                ]
            }
        )
        outcome, _, _ = self.run_loop(factory)
        self.assertFalse(ReviewFixPolicy().escalation_enabled)
        payload = outcome.as_dict()
        self.assertEqual(payload.pop("escalated_findings"), [])
        self.assertEqual(
            payload,
            {
                "protocol": REVIEW_FIX_RESULT_PROTOCOL,
                "status": "succeeded",
                "reason": "review cleared",
                "cycles": 1,
                "risk_tier": outcome.risk_tier,
                "ledger_ref": outcome.ledger_ref,
                "open_finding_keys": [],
                "technical_debt_keys": [],
                "transferred_findings": [],
                "open_findings": [],
                "stop_reason": "cleared",
                "scope_screening": {
                    "screened_count": 0,
                    "screened_finding_keys": [],
                    "by_class": {},
                    "required_finding_keys": [],
                },
            },
        )

    def test_escalation_disabled_leaves_every_record_escalation_reason_empty(self):
        """AC-CC08-1: no ledger record gains a non-empty escalation_reason."""

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
                        attempt.attempt_id, "review-fix-review/1", findings=(finding,)
                    ),
                    lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
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
        outcome, _, evidence = self.run_loop(factory)
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.escalated_findings, ())
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["escalation_reason"], "")
        fix_context = next(
            context for stage, context in factory.calls if stage == "fix"
        )
        self.assertNotIn(
            "optional_details", fix_context["output_contract"]
        )

    def test_escalation_enabled_advertises_unresolvable_finding_keys_to_fixer(self):
        """A fixer only sees the optional contract when escalation is on."""

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
                        attempt.attempt_id, "review-fix-review/1", findings=(finding,)
                    ),
                    lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
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
        outcome, _, _ = self.run_loop(
            factory, policy=ReviewFixPolicy(escalation_enabled=True)
        )
        self.assertEqual(outcome.status, "succeeded")
        fix_context = next(
            context for stage, context in factory.calls if stage == "fix"
        )
        self.assertEqual(
            fix_context["output_contract"]["optional_details"],
            {"unresolvable_finding_keys": "list[string]"},
        )

    def test_escalation_enabled_escalates_unrouted_required_path_finding(self):
        """AC-CC08-2: no owner resolves -> escalated, fix never invoked."""

        finding = {
            "id": "cross-node",
            "statement": "Needs another node's file changed.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "cross node fix",
            "score": 90,
            "fix_cost": "structural",
            "protects": "AC integration",
            "required_paths": ["other.py"],
        }
        key = "feature.txt:cross-node-fix"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1", findings=(finding,)
                    )
                ]
            }
        )
        outcome, _, evidence = self.run_loop(
            factory,
            policy=ReviewFixPolicy(escalation_enabled=True),
        )
        self.assertEqual(outcome.status, "blocked", outcome.reason)
        self.assertEqual([call[0] for call in factory.calls], ["review"])
        self.assertEqual(len(outcome.escalated_findings), 1)
        escalated = outcome.escalated_findings[0]
        self.assertEqual(escalated["key"], key)
        self.assertEqual(escalated["outcome"], "escalated")
        self.assertEqual(
            escalated["escalation_reason"], "required_paths_outside_grant"
        )
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "escalated")
        self.assertNotIn(key, ledger["cycles"][0]["fix_keys"])

    def test_transfer_takes_precedence_over_escalation(self):
        """AC-CC08-3: a resolvable owner claims the finding before escalation."""

        finding = {
            "id": "cross-node",
            "statement": "Needs another node's file changed.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "cross node fix",
            "score": 90,
            "fix_cost": "structural",
            "protects": "AC integration",
            "required_paths": ["other.py"],
        }
        key = "feature.txt:cross-node-fix"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id, "review-fix-review/1", findings=(finding,)
                    )
                ]
            }
        )
        outcome, _, _ = self.run_loop(
            factory,
            policy=ReviewFixPolicy(escalation_enabled=True),
            finding_transfer_targets={"other.py": "B"},
            origin_node_id="A",
        )
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.escalated_findings, ())
        self.assertEqual(len(outcome.transferred_findings), 1)
        transfer = outcome.transferred_findings[0]
        self.assertEqual(transfer["key"], key)
        self.assertEqual(transfer["outcome"], "transferred")
        self.assertEqual(transfer["transferred_to"], "B")
        self.assertEqual(transfer["escalation_reason"], "")

    def test_fixer_declares_a_finding_unresolvable(self):
        """AC-CC08-4: a declared-unresolvable key is escalated, not fixed."""

        finding_a = {
            "id": "cross-node",
            "statement": "Needs another node's file changed.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "cross node fix",
            "score": 90,
            "fix_cost": "structural",
            "protects": "AC integration",
        }
        finding_b = {
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
        key_a = "feature.txt:cross-node-fix"
        key_b = "feature.txt:wrong-value"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding_a, finding_b),
                    ),
                    lambda attempt: result(attempt.attempt_id, "review-fix-review/1"),
                ],
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={
                            "addressed_finding_keys": [key_b],
                            "unresolvable_finding_keys": [key_a],
                        },
                    )
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": [key_b]},
                    )
                ],
            }
        )
        outcome, _, evidence = self.run_loop(
            factory,
            policy=ReviewFixPolicy(escalation_enabled=True),
        )
        self.assertEqual(outcome.status, "blocked", outcome.reason)
        self.assertEqual(len(outcome.escalated_findings), 1)
        escalated = outcome.escalated_findings[0]
        self.assertEqual(escalated["key"], key_a)
        self.assertEqual(
            escalated["escalation_reason"], "fixer_declared_unresolvable"
        )
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key_a]["outcome"], "escalated")
        self.assertEqual(ledger["findings"][key_b]["outcome"], "fixed")

    def test_fixer_unresolvable_key_outside_fix_list_raises_named_error(self):
        """AC-CC08-4: a key outside fix_finding_keys is a protocol violation."""

        ledger = ReviewLedger(
            ReviewFixPolicy(escalation_enabled=True),
            "mechanical",
            allowed_paths=("feature.txt",),
        )
        ledger.findings["feature.txt:known"] = {
            "key": "feature.txt:known",
            "outcome": "open",
            "outcome_reason": "",
            "escalation_reason": "",
        }
        with self.assertRaisesRegex(ReviewFixError, "feature.txt:unknown"):
            ledger.mark_unresolvable(
                ["feature.txt:known"], ["feature.txt:unknown"]
            )

    def test_bounded_fix_only_runs_exactly_fix_then_verify(self):
        """AC-CC08-5: no review stage, no ingest, exactly two stage calls."""

        key = "feature.txt:wrong-value"
        inherited = {
            "key": key,
            "file": "feature.txt",
            "subject": "wrong value",
            "statement": "The value is reversed.",
            "category": "correctness",
            "severity": "major",
            "score": 90,
            "fix_cost": "local",
            "protects": "acceptance criterion correct",
            "requires_disposition": True,
            "contract_violation": False,
            "scope_expanding": False,
            "required_paths": [],
        }
        factory = _Factory(
            {
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
        outcome, _, evidence = self.run_loop(
            factory,
            bounded_fix_only=True,
            seeded_fix_keys=(key,),
            inherited_findings=(inherited,),
        )
        self.assertEqual(outcome.status, "succeeded", outcome.reason)
        self.assertEqual(outcome.cycles, 1)
        self.assertEqual([call[0] for call in factory.calls], ["fix", "verify"])
        self.assertEqual(factory.calls[0][1]["fix_finding_keys"], [key])
        self.assertEqual(factory.calls[1][1]["fix_finding_keys"], [key])
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(list(ledger["findings"].keys()), [key])
        self.assertEqual(ledger["findings"][key]["outcome"], "fixed")
        self.assertNotIn("review_attempt_id", ledger["cycles"][0])
        self.assertTrue(
            all(
                not str(value).endswith("/review")
                for value in ledger["cycles"][0].values()
                if isinstance(value, str)
            )
        )

    def test_bounded_fix_only_escalated_required_finding_blocks(self):
        """AC-CC08-2/AC-CC08-5: escalation must block the bounded fix-only exit.

        ``_run_bounded_fix_only`` seals with its own ``open_required`` check
        at review_fix.py:1171-1178, separate from the main loop's, so it
        needs its own ``escalated_required`` guard too.
        """

        key = "feature.txt:wrong-value"
        inherited = {
            "key": key,
            "file": "feature.txt",
            "subject": "wrong value",
            "statement": "The value is reversed.",
            "category": "correctness",
            "severity": "major",
            "score": 90,
            "fix_cost": "local",
            "protects": "acceptance criterion correct",
            "requires_disposition": True,
            "contract_violation": False,
            "scope_expanding": False,
            "required_paths": [],
        }
        factory = _Factory(
            {
                "fix": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-fix/1",
                        details={
                            "addressed_finding_keys": [],
                            "unresolvable_finding_keys": [key],
                        },
                    )
                ],
                "verify": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-verify/1",
                        details={"verified_finding_keys": []},
                    )
                ],
            }
        )
        outcome, _, evidence = self.run_loop(
            factory,
            policy=ReviewFixPolicy(escalation_enabled=True),
            bounded_fix_only=True,
            seeded_fix_keys=(key,),
            inherited_findings=(inherited,),
        )
        self.assertEqual(outcome.status, "blocked", outcome.reason)
        self.assertEqual(len(outcome.escalated_findings), 1)
        self.assertEqual(outcome.escalated_findings[0]["key"], key)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(ledger["findings"][key]["outcome"], "escalated")

    def test_cycle_limit_debt_sink_still_blocks_on_escalated_required_finding(self):
        """AC-CC08-2: ``_limit_exit``'s debt-sink path needs its own guard too.

        Its exit only consulted ``open_all()`` (review_fix.py:1359-1376),
        which an escalated finding is not a member of, so a cycle-limit exit
        could seal ``succeeded`` with an undischarged required obligation
        the same way the "review cleared" paths used to.
        """

        escalated_finding = {
            "id": "cross-node",
            "statement": "Needs another node's file changed.",
            "category": "integration",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "cross node fix",
            "score": 90,
            "fix_cost": "structural",
            "protects": "AC integration",
            "required_paths": ["other.py"],
        }
        optional_finding = {
            "id": "optional-polish",
            "statement": "The formatting is inconsistent.",
            "category": "style",
            "severity": "minor",
            "requires_disposition": False,
            "file": "feature.txt",
            "subject": "polish formatting",
            "score": 90,
            "fix_cost": "local",
            "protects": "style guide",
        }
        escalated_key = "feature.txt:cross-node-fix"
        factory = _Factory(
            {
                "review": [
                    lambda attempt: result(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(escalated_finding, optional_finding),
                    )
                ],
            }
        )
        policy = ReviewFixPolicy(
            escalation_enabled=True,
            mechanical_cycle_limit=1,
            technical_debt_sink_enabled=True,
        )

        outcome, _, evidence = self.run_loop(factory, policy=policy)

        self.assertEqual(outcome.status, "blocked", outcome.reason)
        self.assertEqual(len(outcome.escalated_findings), 1)
        self.assertEqual(outcome.escalated_findings[0]["key"], escalated_key)
        ledger = json.loads(evidence.open(outcome.ledger_ref))
        self.assertEqual(
            ledger["findings"][escalated_key]["outcome"], "escalated"
        )
        self.assertEqual(
            ledger["findings"]["feature.txt:polish-formatting"]["outcome"], "debt"
        )

    def test_bounded_fix_only_requires_seeded_fix_keys(self):
        with self.assertRaisesRegex(ValueError, "seeded_fix_keys"):
            self.build_loop(
                _Factory({}),
                bounded_fix_only=True,
            )

    def test_seeded_fix_keys_requires_bounded_fix_only(self):
        with self.assertRaisesRegex(ValueError, "bounded_fix_only"):
            self.build_loop(
                _Factory({}),
                seeded_fix_keys=("feature.txt:x",),
            )


if __name__ == "__main__":
    unittest.main()
