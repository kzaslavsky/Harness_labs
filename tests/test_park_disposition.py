"""A parked fix worker's out-of-fence disposition must survive to the failure.

A review-fix worker that honestly declines to change the repository -- the
assigned finding's sole required path lies outside the controller-supplied
write fence -- states why in its final structured output: a finding flagged
``scope_expanding`` with ``required_paths``, plus an unresolved question
asking the controller to widen the grant.  Before this reporting path
existed, that disposition was discarded at the ``LiveExecutionError`` raise
site and every operator saw only "writable worker completed without changing
the repository: LiveExecutionError" (three real occurrences on 2026-08-21 in
logs/runs/uc1-graph/flow-editor-uc1-coreloop-attempt-{1,2}-UC-1A).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.core.attempts import TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import (
    CodexSemanticTaskExecutor,
    PARK_DISPOSITION_PROTOCOL,
    extract_park_disposition,
)
from harness_labs.core.controller_results import semantic_payload
from harness_labs.featurerun.review_fix import (
    ReviewFixLoop,
    ReviewFixPolicy,
)
from harness_labs.plangraph.plan_graph import _review_fix_park_reason


def _parked_raw_output() -> dict[str, object]:
    """The shape a real parked fix worker emitted (attempt-1-UC-1A)."""

    return {
        "summary": (
            "Targeted tests are green, but the exact ledger finding remains "
            "open because its required execution route is outside the "
            "authorized paths."
        ),
        "deliverable_markdown": "The assigned finding is parked, not fixed.",
        "details_json": json.dumps(
            {
                "schema_identity": "review-fix-fix/1",
                "addressed_finding_keys": [],
                "parked_questions": [
                    {
                        "finding_key": "tests/test_l2_batch.py:batch-gate",
                        "reason": (
                            "The exact fix requires an out-of-grant file."
                        ),
                    }
                ],
            }
        ),
        "claims": [],
        "findings": [
            {
                "id": "F-PARK-1",
                "category": "scope/contract",
                "severity": "major",
                "score": 96,
                "fix_cost": "structural",
                "contract_violation": True,
                "requires_disposition": True,
                "scope_expanding": True,
                "file": "tests/test_l2_batch.py",
                "subject": "execution-import-root-gate",
                "protects": "FED-01-server execution-time refusal",
                "required_paths": ["tests/test_l2_batch.py"],
                "new_evidence": "Route inspection shows the bypass.",
                "statement": (
                    "The exact fix cannot be made because its sole required "
                    "file is outside the controller-supplied write fence"
                ),
            }
        ],
        "recommendations": [],
        "unresolved_questions": [
            "May the controller widen allowed_paths to include "
            "tests/test_l2_batch.py?"
        ],
        "satisfied_criteria": [],
    }


class ExtractParkDispositionTests(unittest.TestCase):
    def test_reads_the_real_worker_shape(self) -> None:
        disposition = extract_park_disposition(
            _parked_raw_output(), ("retinology/web/_l2_pipelines.py",)
        )
        assert disposition is not None
        self.assertEqual(disposition["protocol"], PARK_DISPOSITION_PROTOCOL)
        (finding,) = disposition["findings"]
        self.assertEqual(finding["id"], "F-PARK-1")
        self.assertEqual(finding["subject"], "execution-import-root-gate")
        self.assertEqual(finding["required_paths"], ["tests/test_l2_batch.py"])
        self.assertEqual(
            finding["out_of_fence_paths"], ["tests/test_l2_batch.py"]
        )
        self.assertTrue(finding["scope_expanding"])
        self.assertIn("write fence", finding["statement"])
        self.assertEqual(len(disposition["unresolved_questions"]), 1)
        self.assertEqual(
            disposition["parked_questions"][0]["finding_key"],
            "tests/test_l2_batch.py:batch-gate",
        )
        self.assertIn("summary", disposition)

    def test_out_of_fence_required_paths_park_even_without_flags(self) -> None:
        raw = _parked_raw_output()
        finding = raw["findings"][0]
        finding["scope_expanding"] = False
        finding["requires_disposition"] = False
        del raw["details_json"]
        disposition = extract_park_disposition(raw, ("src",))
        assert disposition is not None
        self.assertEqual(
            disposition["findings"][0]["out_of_fence_paths"],
            ["tests/test_l2_batch.py"],
        )

    def test_unexplained_no_change_output_yields_none(self) -> None:
        raw = {
            "summary": "Done.",
            "findings": [],
            "unresolved_questions": [],
            "details_json": "{}",
        }
        self.assertIsNone(extract_park_disposition(raw, ("src",)))
        self.assertIsNone(extract_park_disposition(None, ("src",)))
        self.assertIsNone(extract_park_disposition("not a mapping", ("src",)))

    def test_malformed_details_json_is_tolerated(self) -> None:
        raw = _parked_raw_output()
        raw["details_json"] = "{not json"
        disposition = extract_park_disposition(raw, ("src",))
        assert disposition is not None
        self.assertNotIn("parked_questions", disposition)
        self.assertEqual(len(disposition["findings"]), 1)


class ExecutorParkPayloadTests(unittest.TestCase):
    def test_no_change_failure_carries_the_park_disposition(self) -> None:
        task = {
            "id": "fix",
            "objective": "Fix the finding",
            "context": "{}",
            "details_schema": "review-fix-fix/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Fix precisely.",
            sandbox="workspace-write",
            writable_paths=("retinology/web/_l2_pipelines.py",),
            require_repository_change=True,
        )
        unchanged = {
            "head": "abc",
            "branch": "feature",
            "changed_paths": [],
            "files": {},
        }
        raw = _parked_raw_output()

        def run(argv, **kwargs):
            if argv[0] == "git":
                # Baseline restoration probes after the failed attempt are
                # not this test's subject; treat them as no-op successes.
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(json.dumps(raw), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch(
                "harness_labs.core.controller_live.shutil.which",
                return_value="codex",
            ),
            patch(
                "harness_labs.core.controller_live.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.controller_live.workspace_snapshot",
                side_effect=(dict(unchanged), dict(unchanged)),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            result = executor.execute(
                TaskAttempt("fix/attempt-1", "task:fix", "context:fix", "grant")
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.payload["error"],
            "writable worker completed without changing the repository",
        )
        disposition = result.payload["park_disposition"]
        self.assertEqual(disposition["protocol"], PARK_DISPOSITION_PROTOCOL)
        self.assertEqual(
            disposition["findings"][0]["out_of_fence_paths"],
            ["tests/test_l2_batch.py"],
        )


def _semantic(attempt_id, schema, *, findings=(), details=None):
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


def _parked_fix_failure(attempt) -> TaskResult:
    return TaskResult(
        attempt.attempt_id,
        "failed",
        {
            "error": "writable worker completed without changing the repository",
            "error_type": "LiveExecutionError",
            "park_disposition": {
                "protocol": PARK_DISPOSITION_PROTOCOL,
                "findings": [
                    {
                        "id": "F-PARK-1",
                        "subject": "execution-import-root-gate",
                        "file": "tests/test_l2_batch.py",
                        "statement": (
                            "The exact fix cannot be made because its sole "
                            "required file is outside the controller-supplied "
                            "write fence"
                        ),
                        "severity": "major",
                        "required_paths": ["tests/test_l2_batch.py"],
                        "out_of_fence_paths": ["tests/test_l2_batch.py"],
                        "scope_expanding": True,
                    }
                ],
                "unresolved_questions": [
                    "May the controller widen allowed_paths to include "
                    "tests/test_l2_batch.py?"
                ],
            },
        },
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


class ReviewFixParkPropagationTests(unittest.TestCase):
    def run_loop(self, factory):
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
            changed_paths=("feature.txt",),
            executor_factory=factory,
            evidence=evidence,
            audit=audit,
            policy=ReviewFixPolicy(),
        )
        return loop.run(), audit, evidence

    def test_parked_fix_surfaces_the_actionable_reason(self) -> None:
        finding = {
            "id": "wrong",
            "statement": "The gate is bypassed.",
            "category": "correctness",
            "severity": "major",
            "requires_disposition": True,
            "file": "feature.txt",
            "subject": "execution gate",
            "score": 90,
            "fix_cost": "local",
            "protects": "acceptance criterion correct",
        }
        factory = _Factory(
            {
                "review": [
                    lambda attempt: _semantic(
                        attempt.attempt_id,
                        "review-fix-review/1",
                        findings=(finding,),
                    ),
                ],
                # Both the first fix attempt and its one recovery attempt
                # park honestly, exactly as the three real 2026-08-21 runs
                # did.
                "fix": [_parked_fix_failure, _parked_fix_failure],
            }
        )

        outcome, audit, _ = self.run_loop(factory)

        self.assertEqual(outcome.status, "failed")
        self.assertIn("fix blocked", outcome.reason)
        self.assertIn(
            "finding execution-import-root-gate requires out-of-fence path "
            "tests/test_l2_batch.py",
            outcome.reason,
        )
        self.assertIn("unresolved: May the controller widen", outcome.reason)
        # The opaque payload repr never leaks into the reason.
        self.assertNotIn("LiveExecutionError", outcome.reason)
        assert outcome.park_disposition is not None
        self.assertEqual(
            outcome.park_disposition["findings"][0]["out_of_fence_paths"],
            ["tests/test_l2_batch.py"],
        )
        exported = outcome.as_dict()
        self.assertEqual(
            exported["park_disposition"], dict(outcome.park_disposition)
        )
        events = [
            json.loads(line)
            for line in (audit.run_dir / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        triggered = [
            event
            for event in events
            if event["event_type"] == "review_fix_recovery_triggered"
        ]
        self.assertEqual(len(triggered), 1)
        self.assertEqual(
            triggered[0]["payload"]["park_disposition"]["findings"][0]["id"],
            "F-PARK-1",
        )
        failed = [
            event
            for event in events
            if event["event_type"] == "review_fix_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("park_disposition", failed[0]["payload"])

    def test_unparked_results_stay_byte_identical(self) -> None:
        factory = _Factory(
            {
                "review": [
                    lambda attempt: _semantic(
                        attempt.attempt_id, "review-fix-review/1"
                    ),
                ],
            }
        )
        outcome, _, _ = self.run_loop(factory)
        self.assertEqual(outcome.status, "succeeded")
        self.assertIsNone(outcome.park_disposition)
        self.assertNotIn("park_disposition", outcome.as_dict())


class PlanGraphParkReasonTests(unittest.TestCase):
    def test_blocked_evidence_with_park_yields_the_readable_reason(self) -> None:
        evidence = {
            "review_fix": {
                "status": "blocked",
                "reason": (
                    "fix attempt ended with status failed: fix blocked: "
                    "finding execution-import-root-gate requires "
                    "out-of-fence path tests/test_l2_batch.py"
                ),
                "park_disposition": {
                    "protocol": PARK_DISPOSITION_PROTOCOL,
                    "findings": [],
                    "unresolved_questions": [],
                },
            }
        }
        reason = _review_fix_park_reason(evidence)
        assert reason is not None
        self.assertIn("out-of-fence path tests/test_l2_batch.py", reason)

    def test_evidence_without_park_changes_nothing(self) -> None:
        self.assertIsNone(_review_fix_park_reason(None))
        self.assertIsNone(_review_fix_park_reason({}))
        self.assertIsNone(
            _review_fix_park_reason(
                {"review_fix": {"status": "blocked", "reason": "cycle limit"}}
            )
        )


if __name__ == "__main__":
    unittest.main()
