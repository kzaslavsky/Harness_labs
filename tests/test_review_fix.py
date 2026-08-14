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
