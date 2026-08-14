"""Finding test for PlanGraph node CB2-01: claims vocabulary becomes annotation.

Item 11 of ``docs/development/contract-burden-reduction.md``: review/fix/verify
tasks are dispatched with ``acceptance_criteria=[]``, but a worker naturally
echoes criterion ids or prose in its ``satisfied_criteria`` claim. At the
frozen base harness, both live executors treat that vocabulary itself as
claiming unassigned authority and fail the node with
``worker claimed unassigned criteria: [...]`` even though the node's gate has
already passed. This file is self-contained and imports only symbols that
exist at the frozen base harness, so it must exercise real entry points
(``CodexSemanticTaskExecutor.execute`` / ``ClaudeSemanticTaskExecutor.execute``)
rather than anything CB2-01 itself introduces.

At the base harness, an out-of-assignment ``satisfied_criteria`` entry kills
the task outright for both executors. At the candidate, the same input
completes the task: the out-of-assignment ids are dropped from the recorded
``criterion_coverage``, journaled as a distinct annotation event carrying the
dropped ids, and in-assignment ids are recorded exactly as before.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.attempts import TaskAttempt
from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.claude_task_executor import ClaudeSemanticTaskExecutor
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_live import CodexSemanticTaskExecutor
from harness_labs.controller_results import validate_semantic_result


_SUMMARY = "The review completed and every observation is recorded below."
_DELIVERABLE = "# Review\nEvidence-backed observations with real content.\n"


def _raw_result(*, satisfied_criteria: list) -> dict:
    return {
        "summary": _SUMMARY,
        "deliverable_markdown": _DELIVERABLE,
        "details_json": json.dumps({"head": "abc"}),
        "claims": [],
        "findings": [],
        "recommendations": [],
        "unresolved_questions": [],
        "satisfied_criteria": satisfied_criteria,
    }


def _claude_envelope(raw: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(raw),
        "structured_output": raw,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 100,
            "output_tokens": 50,
        },
        "total_cost_usd": 0.01,
        "permission_denials": [],
    }


def _snapshot() -> dict:
    return {"head": "abc", "branch": "feature", "changed_paths": [], "files": {}}


def _init_repo(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    return repository


def _task(*, acceptance_criteria: list) -> dict:
    return {
        "id": "review",
        "objective": "Review the candidate",
        "context": json.dumps({"artifact_kind": "review-report"}),
        "details_schema": "review-details/1",
        "acceptance_criteria": acceptance_criteria,
        "required_capabilities": ["repo.read"],
    }


def _open_audit_journal(root: Path) -> AuditJournal:
    return AuditJournal(
        root / "audit-run",
        "run",
        actor=AuditActor("controller", "controller"),
    )


def _journaled_events(journal: AuditJournal, event_type: str) -> list[dict]:
    rows = [
        json.loads(line)
        for line in journal.events_path.read_text(encoding="utf-8").splitlines()
    ]
    return [row for row in rows if row["event_type"] == event_type]


class CodexUnassignedClaimTests(unittest.TestCase):
    """The Codex semantic executor's handling of out-of-assignment claims."""

    def _run_executor(
        self, raw: dict, task: dict, *, audit: AuditJournal | None = None
    ):
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "review/attempt-1",
                "task:review",
                "context:review",
                "profile:reviewer",
            )

            def run(argv, **kwargs):
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout='{"type":"turn.completed"}\n',
                    stderr="",
                )

            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Review precisely.",
                audit=audit,
            )
            with (
                patch(
                    "harness_labs.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch(
                    "harness_labs.controller_live.subprocess.run",
                    side_effect=run,
                ),
            ):
                return executor.execute(attempt)

    def test_review_shaped_dispatch_with_unassigned_claim_completes(self) -> None:
        # Review-shaped dispatch: no acceptance criteria assigned to the task,
        # but the worker echoes a criterion id anyway.
        raw = _raw_result(satisfied_criteria=["fixed-the-bug"])
        task = _task(acceptance_criteria=[])
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result = self._run_executor(raw, task, audit=journal)
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result,
                expected_details_schema="review-details/1",
            )
            self.assertEqual(semantic.criterion_coverage, ())
            events = _journaled_events(journal, "unassigned_criteria_annotated")
            self.assertEqual(len(events), 1, events)
            self.assertEqual(events[0]["status"], "succeeded")
            self.assertEqual(events[0]["attempt_id"], "review/attempt-1")
            self.assertEqual(
                events[0]["payload"], {"dropped_criteria": ["fixed-the-bug"]}
            )

    def test_mixed_claim_keeps_assigned_and_drops_unassigned(self) -> None:
        raw = _raw_result(satisfied_criteria=["reviewed", "fixed-the-bug"])
        task = _task(acceptance_criteria=["reviewed"])
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result = self._run_executor(raw, task, audit=journal)
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result,
                expected_details_schema="review-details/1",
            )
            self.assertEqual(len(semantic.criterion_coverage), 1)
            self.assertEqual(
                semantic.criterion_coverage[0]["criterion_id"], "reviewed"
            )
            self.assertEqual(semantic.criterion_coverage[0]["status"], "satisfied")
            events = _journaled_events(journal, "unassigned_criteria_annotated")
            self.assertEqual(len(events), 1, events)
            self.assertEqual(
                events[0]["payload"], {"dropped_criteria": ["fixed-the-bug"]}
            )


class ClaudeUnassignedClaimTests(unittest.TestCase):
    """The Claude semantic executor's handling of out-of-assignment claims."""

    def _run_executor(
        self, raw: dict, task: dict, *, audit: AuditJournal | None = None
    ):
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "review/attempt-1",
                "task:review",
                "context:review",
                "profile:reviewer",
            )

            def run(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(_claude_envelope(raw)),
                    stderr="",
                )

            executor = ClaudeSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Review precisely.",
                audit=audit,
            )
            with (
                patch(
                    "harness_labs.claude_task_executor.shutil.which",
                    return_value="claude",
                ),
                patch(
                    "harness_labs.claude_task_executor.subprocess.run",
                    side_effect=run,
                ),
                patch(
                    "harness_labs.claude_task_executor.workspace_snapshot",
                    side_effect=(_snapshot(), _snapshot()),
                ),
            ):
                return executor.execute(attempt)

    def test_review_shaped_dispatch_with_unassigned_claim_completes(self) -> None:
        raw = _raw_result(satisfied_criteria=["fixed-the-bug"])
        task = _task(acceptance_criteria=[])
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result = self._run_executor(raw, task, audit=journal)
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result,
                expected_details_schema="review-details/1",
            )
            self.assertEqual(semantic.criterion_coverage, ())
            events = _journaled_events(journal, "unassigned_criteria_annotated")
            self.assertEqual(len(events), 1, events)
            self.assertEqual(events[0]["status"], "succeeded")
            self.assertEqual(events[0]["attempt_id"], "review/attempt-1")
            self.assertEqual(
                events[0]["payload"], {"dropped_criteria": ["fixed-the-bug"]}
            )

    def test_mixed_claim_keeps_assigned_and_drops_unassigned(self) -> None:
        raw = _raw_result(satisfied_criteria=["reviewed", "fixed-the-bug"])
        task = _task(acceptance_criteria=["reviewed"])
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result = self._run_executor(raw, task, audit=journal)
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result,
                expected_details_schema="review-details/1",
            )
            self.assertEqual(len(semantic.criterion_coverage), 1)
            self.assertEqual(
                semantic.criterion_coverage[0]["criterion_id"], "reviewed"
            )
            self.assertEqual(semantic.criterion_coverage[0]["status"], "satisfied")
            events = _journaled_events(journal, "unassigned_criteria_annotated")
            self.assertEqual(len(events), 1, events)
            self.assertEqual(
                events[0]["payload"], {"dropped_criteria": ["fixed-the-bug"]}
            )


if __name__ == "__main__":
    unittest.main()
