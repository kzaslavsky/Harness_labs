"""Finding test for PlanGraph node CB-07: deliverable-content floor.

Orbit exp-1's originating defect: ``"summary": "test"`` passed the typed
result contract (``minLength: 1`` in the raw-output schema and no content
check in ``validate_semantic_result``) and only the coordinator's judgment
refused it. This file is self-contained and imports only symbols that exist
at the frozen base harness, so it must exercise real entry points rather than
anything CB-07 itself introduces.

At the base harness, a placeholder ``summary``/``deliverable_markdown`` is
accepted both by ``validate_semantic_result`` directly and by the
``CodexSemanticTaskExecutor`` / ``ClaudeSemanticTaskExecutor`` entry points
(with subprocess calls fully mocked, so no real model process is invoked).
At the candidate, the same inputs are refused mechanically at the shared
result boundary for both executors.
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
from harness_labs.core.claude_task_executor import ClaudeSemanticTaskExecutor
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import CodexSemanticTaskExecutor
from harness_labs.core.controller_results import (
    SemanticResultError,
    semantic_payload,
    validate_semantic_result,
)


_SUBSTANTIVE_SUMMARY = (
    "The repository inspection completed and every assigned criterion is "
    "backed by cited evidence."
)
_SUBSTANTIVE_DELIVERABLE = "# Inspection\nEvidence-backed result with real content.\n"

# Sub-minimal length, known placeholder tokens (case/whitespace-insensitive),
# and a single token repeated across the whole field.
_PLACEHOLDER_FIELDS = ("test", "todo", "TBD", "n/a", "  Placeholder  ", "x x x x x")


def _raw_result(**overrides: object) -> dict:
    raw = {
        "summary": _SUBSTANTIVE_SUMMARY,
        "deliverable_markdown": _SUBSTANTIVE_DELIVERABLE,
        "details_json": json.dumps({"head": "abc"}),
        "claims": [],
        "findings": [],
        "recommendations": [],
        "unresolved_questions": [],
        "satisfied_criteria": [],
    }
    raw.update(overrides)
    return raw


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


def _task(details_schema: str = "inspection-details/1") -> dict:
    return {
        "id": "inspect",
        "objective": "Inspect the repository",
        "context": json.dumps({"artifact_kind": "inspection-report"}),
        "details_schema": details_schema,
        "acceptance_criteria": [],
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


class SharedResultBoundaryFloorTests(unittest.TestCase):
    """`validate_semantic_result` is the shared boundary both executors call."""

    def test_placeholder_summary_is_refused_by_validate_semantic_result(self) -> None:
        for placeholder in _PLACEHOLDER_FIELDS:
            with self.subTest(summary=placeholder):
                payload = semantic_payload(
                    summary=placeholder,
                    details_schema="inspection-details/1",
                    details={"head": "abc"},
                )
                result = TaskResult(
                    attempt_id="attempt-1",
                    status="succeeded",
                    payload=payload,
                )
                with self.assertRaises(SemanticResultError):
                    validate_semantic_result(
                        result,
                        expected_details_schema="inspection-details/1",
                    )

    def test_substantive_summary_passes_unchanged(self) -> None:
        payload = semantic_payload(
            summary=_SUBSTANTIVE_SUMMARY,
            details_schema="inspection-details/1",
            details={"head": "abc"},
        )
        result = TaskResult(attempt_id="attempt-2", status="succeeded", payload=payload)
        semantic = validate_semantic_result(
            result,
            expected_details_schema="inspection-details/1",
        )
        self.assertEqual(semantic.summary, _SUBSTANTIVE_SUMMARY)


class CodexExecutorFloorTests(unittest.TestCase):
    """The Codex semantic executor refuses a placeholder result mechanically."""

    def _run_executor(
        self, raw: dict, *, audit: AuditJournal | None = None
    ) -> TaskResult:
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "inspect/attempt-1",
                "task:inspect",
                "context:inspect",
                "profile:inspector",
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
                _task(),
                repository,
                evidence,
                "Inspect precisely.",
                audit=audit,
            )
            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch(
                    "harness_labs.core.controller_live.subprocess.run",
                    side_effect=run,
                ),
            ):
                return executor.execute(attempt)

    def test_placeholder_deliverable_is_refused(self) -> None:
        raw = _raw_result(summary="test", deliverable_markdown="test")
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result = self._run_executor(raw, audit=journal)
            self.assertEqual(result.status, "failed", result.payload)
            self.assertEqual(
                result.payload.get("error_type"), "DeliverableFloorViolation"
            )
            self.assertEqual(result.payload.get("field"), "deliverable_markdown")
            self.assertEqual(result.payload.get("reason"), "placeholder_token")
            events = _journaled_events(journal, "deliverable_floor_refused")
            self.assertEqual(len(events), 1, events)
            self.assertEqual(events[0]["status"], "failed")
            self.assertEqual(events[0]["attempt_id"], "inspect/attempt-1")
            self.assertEqual(
                events[0]["payload"],
                {"field": "deliverable_markdown", "reason": "placeholder_token"},
            )

    def test_substantive_deliverable_passes_unchanged(self) -> None:
        raw = _raw_result()
        result = self._run_executor(raw)
        self.assertEqual(result.status, "succeeded", result.payload)
        semantic = validate_semantic_result(
            result,
            expected_details_schema="inspection-details/1",
        )
        self.assertEqual(semantic.summary, _SUBSTANTIVE_SUMMARY)


class ClaudeExecutorFloorTests(unittest.TestCase):
    """The Claude semantic executor refuses a placeholder result mechanically."""

    def _run_executor(
        self, raw: dict, *, audit: AuditJournal | None = None
    ) -> TaskResult:
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "inspect/attempt-1",
                "task:inspect",
                "context:inspect",
                "profile:inspector",
            )

            def run(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(_claude_envelope(raw)),
                    stderr="",
                )

            executor = ClaudeSemanticTaskExecutor(
                _task(),
                repository,
                evidence,
                "Inspect precisely.",
                audit=audit,
            )
            with (
                patch(
                    "harness_labs.core.claude_task_executor.shutil.which",
                    return_value="claude",
                ),
                patch(
                    "harness_labs.core.claude_task_executor.subprocess.run",
                    side_effect=run,
                ),
                patch(
                    "harness_labs.core.claude_task_executor.workspace_snapshot",
                    side_effect=(_snapshot(), _snapshot()),
                ),
            ):
                return executor.execute(attempt)

    def test_placeholder_deliverable_is_refused(self) -> None:
        raw = _raw_result(summary="todo", deliverable_markdown="todo")
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result = self._run_executor(raw, audit=journal)
            self.assertEqual(result.status, "failed", result.payload)
            self.assertEqual(
                result.payload.get("error_type"), "DeliverableFloorViolation"
            )
            self.assertEqual(result.payload.get("field"), "deliverable_markdown")
            self.assertEqual(result.payload.get("reason"), "placeholder_token")
            events = _journaled_events(journal, "deliverable_floor_refused")
            self.assertEqual(len(events), 1, events)
            self.assertEqual(events[0]["status"], "failed")
            self.assertEqual(events[0]["attempt_id"], "inspect/attempt-1")
            self.assertEqual(
                events[0]["payload"],
                {"field": "deliverable_markdown", "reason": "placeholder_token"},
            )

    def test_substantive_deliverable_passes_unchanged(self) -> None:
        raw = _raw_result()
        result = self._run_executor(raw)
        self.assertEqual(result.status, "succeeded", result.payload)
        semantic = validate_semantic_result(
            result,
            expected_details_schema="inspection-details/1",
        )
        self.assertEqual(semantic.summary, _SUBSTANTIVE_SUMMARY)


if __name__ == "__main__":
    unittest.main()
