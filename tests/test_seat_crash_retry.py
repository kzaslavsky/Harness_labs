"""In-dispatch retry for the two seat-crash classes sibling to CB-07's floor retry.

CB-07 (see ``tests/test_relax_semantic_floor.py``) gave ``DeliverableFloorViolation``
one bounded in-dispatch retry. Two sibling crash classes still failed the whole
node while verification was green and the code work was done:

1. Typed semantic shape errors: ``validate_semantic_result`` raising a
   ``SemanticResultError`` subclass other than ``DeliverableFloorViolation``
   (e.g. an invalid ``addressed_finding_keys``-style claim/finding shape).
2. CLI-level structured-output exhaustion: the claude CLI exiting status 1
   with ``terminal_reason: "structured_output_retry_exhausted"`` /
   ``subtype: "error_max_structured_output_retries"`` after real turns of
   workspace edits, surfaced as ``LiveExecutionError``.

Both extend the same bounded retry loop CB-07 built (one shared in-dispatch
retry, ``DELIVERABLE_FLOOR_RETRY_LIMIT``) rather than a second loop. Class 1
applies to both live executors; class 2 is Claude-CLI-specific and does not
touch the Codex backend at all.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.core.attempts import TaskAttempt
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.claude_task_executor import (
    ClaudeSemanticTaskExecutor,
    StructuredOutputExhaustionError,
)
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import CodexSemanticTaskExecutor
from harness_labs.core.controller_results import (
    SemanticResultError,
    validate_semantic_result,
)


_SUBSTANTIVE_SUMMARY = (
    "The repository inspection completed and every assigned criterion is "
    "backed by cited evidence."
)
_SUBSTANTIVE_DELIVERABLE = "# Inspection\nEvidence-backed result with real content.\n"


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


def _shape_violating_raw() -> dict:
    """A raw result that passes the schema but fails ``validate_semantic_result``.

    Mirrors the real UC-2D incident: the envelope is well-formed and the
    deliverable content clears the floor, but a claim carries an invalid
    ``kind`` -- the same class of typed shape error as an invalid
    ``addressed_finding_keys`` list, just triggered through the generic
    envelope this test suite already builds.
    """

    return _raw_result(
        claims=[
            {
                "id": "c1",
                "statement": "Something true.",
                "kind": "not-a-real-kind",
                "evidence_refs": [],
            }
        ]
    )


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


def _exhaustion_envelope() -> dict:
    """A claude CLI result envelope for the structured-output-exhaustion failure.

    Field names and values match the real UC-2 authoring-attempt-14 incident
    evidence (000010-plan-graph-node-failure-evidence.json).
    """

    return {
        "type": "result",
        "is_error": True,
        "subtype": "error_max_structured_output_retries",
        "terminal_reason": "structured_output_retry_exhausted",
        "errors": ["Failed to provide valid structured output after 5 attempts"],
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


class CodexExecutorShapeRetryTests(unittest.TestCase):
    """Class 1 (typed semantic shape errors) on the Codex executor."""

    def _run_executor(
        self,
        raws: dict | list[dict],
        *,
        audit: AuditJournal | None = None,
    ):
        raw_sequence = [raws] if isinstance(raws, dict) else list(raws)
        prompts: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "inspect/attempt-1",
                "task:inspect",
                "context:inspect",
                "profile:inspector",
            )
            calls = {"n": 0}

            def run(argv, **kwargs):
                index = min(calls["n"], len(raw_sequence) - 1)
                calls["n"] += 1
                prompts.append(kwargs.get("input", ""))
                raw = raw_sequence[index]
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv, 0, stdout='{"type":"turn.completed"}\n', stderr=""
                )

            executor = CodexSemanticTaskExecutor(
                _task(), repository, evidence, "Inspect precisely.", audit=audit
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
                return executor.execute(attempt), prompts

    def test_shape_error_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result, prompts = self._run_executor(
                [_shape_violating_raw(), _raw_result()], audit=journal
            )
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result, expected_details_schema="inspection-details/1"
            )
            self.assertEqual(semantic.summary, _SUBSTANTIVE_SUMMARY)
            self.assertEqual(len(prompts), 2, prompts)
            self.assertNotIn("Corrective addendum", prompts[0])
            self.assertIn("Corrective addendum", prompts[1])
            self.assertIn("invalid kind", prompts[1])
            refused = _journaled_events(journal, "semantic_shape_refused")
            self.assertEqual(len(refused), 1, refused)
            self.assertEqual(refused[0]["payload"]["violation"], "SemanticResultError")
            self.assertIn("invalid kind", refused[0]["payload"]["message"])
            retried = _journaled_events(journal, "semantic_shape_retry_dispatched")
            self.assertEqual(len(retried), 1, retried)
            self.assertEqual(retried[0]["payload"]["attempt"], 2)

    def test_shape_error_twice_fails_with_original_payload_shape(self) -> None:
        raw = _shape_violating_raw()
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result, prompts = self._run_executor(raw, audit=journal)
            self.assertEqual(result.status, "failed", result.payload)
            self.assertEqual(result.payload.get("error_type"), "SemanticResultError")
            self.assertIn("invalid kind", result.payload.get("error", ""))
            self.assertEqual(len(prompts), 2, prompts)
            refused = _journaled_events(journal, "semantic_shape_refused")
            self.assertEqual(len(refused), 2, refused)
            retried = _journaled_events(journal, "semantic_shape_retry_dispatched")
            self.assertEqual(len(retried), 1, retried)


class ClaudeExecutorShapeRetryTests(unittest.TestCase):
    """Class 1 (typed semantic shape errors) on the Claude executor."""

    def _run_executor(
        self,
        raws: dict | list[dict],
        *,
        audit: AuditJournal | None = None,
    ):
        raw_sequence = [raws] if isinstance(raws, dict) else list(raws)
        prompts: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "inspect/attempt-1",
                "task:inspect",
                "context:inspect",
                "profile:inspector",
            )
            calls = {"n": 0}

            def run(argv, **kwargs):
                index = min(calls["n"], len(raw_sequence) - 1)
                calls["n"] += 1
                prompts.append(kwargs.get("input", ""))
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(_claude_envelope(raw_sequence[index])),
                    stderr="",
                )

            executor = ClaudeSemanticTaskExecutor(
                _task(), repository, evidence, "Inspect precisely.", audit=audit
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
                    return_value=_snapshot(),
                ),
            ):
                return executor.execute(attempt), prompts

    def test_shape_error_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result, prompts = self._run_executor(
                [_shape_violating_raw(), _raw_result()], audit=journal
            )
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result, expected_details_schema="inspection-details/1"
            )
            self.assertEqual(semantic.summary, _SUBSTANTIVE_SUMMARY)
            self.assertEqual(len(prompts), 2, prompts)
            self.assertNotIn("Corrective addendum", prompts[0])
            self.assertIn("Corrective addendum", prompts[1])
            self.assertIn("invalid kind", prompts[1])
            refused = _journaled_events(journal, "semantic_shape_refused")
            self.assertEqual(len(refused), 1, refused)
            retried = _journaled_events(journal, "semantic_shape_retry_dispatched")
            self.assertEqual(len(retried), 1, retried)
            self.assertEqual(retried[0]["payload"]["attempt"], 2)

    def test_shape_error_twice_fails_with_original_payload_shape(self) -> None:
        raw = _shape_violating_raw()
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result, prompts = self._run_executor(raw, audit=journal)
            self.assertEqual(result.status, "failed", result.payload)
            self.assertEqual(result.payload.get("error_type"), "SemanticResultError")
            self.assertIn("invalid kind", result.payload.get("error", ""))
            self.assertEqual(len(prompts), 2, prompts)
            refused = _journaled_events(journal, "semantic_shape_refused")
            self.assertEqual(len(refused), 2, refused)
            retried = _journaled_events(journal, "semantic_shape_retry_dispatched")
            self.assertEqual(len(retried), 1, retried)


class ClaudeExecutorStructuredOutputExhaustionTests(unittest.TestCase):
    """Class 2 (CLI-level structured-output exhaustion). Claude-only -- the
    Codex backend has no equivalent failure shape and is untouched by this
    class of fix.
    """

    def _run_executor(
        self,
        outcomes: list[dict | str],
        *,
        audit: AuditJournal | None = None,
    ):
        """Dispatch a sequence of outcomes: a raw dict succeeds, ``"exhausted"``
        reproduces the claude CLI's own exhaustion failure (exit status 1).
        """

        prompts: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            repository = _init_repo(Path(temporary))
            evidence = EvidenceCatalog()
            attempt = TaskAttempt(
                "inspect/attempt-1",
                "task:inspect",
                "context:inspect",
                "profile:inspector",
            )
            calls = {"n": 0}

            def run(argv, **kwargs):
                index = min(calls["n"], len(outcomes) - 1)
                calls["n"] += 1
                prompts.append(kwargs.get("input", ""))
                outcome = outcomes[index]
                if outcome == "exhausted":
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout=json.dumps(_exhaustion_envelope()),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(_claude_envelope(outcome)), stderr=""
                )

            executor = ClaudeSemanticTaskExecutor(
                _task(), repository, evidence, "Inspect precisely.", audit=audit
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
                    return_value=_snapshot(),
                ),
            ):
                return executor.execute(attempt), prompts

    def test_exhaustion_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result, prompts = self._run_executor(
                ["exhausted", _raw_result()], audit=journal
            )
            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result, expected_details_schema="inspection-details/1"
            )
            self.assertEqual(semantic.summary, _SUBSTANTIVE_SUMMARY)
            self.assertEqual(len(prompts), 2, prompts)
            self.assertNotIn("Corrective addendum", prompts[0])
            self.assertIn("Corrective addendum", prompts[1])
            self.assertIn(
                "Failed to provide valid structured output after 5 attempts",
                prompts[1],
            )
            refused = _journaled_events(
                journal, "structured_output_exhaustion_refused"
            )
            self.assertEqual(len(refused), 1, refused)
            self.assertEqual(
                refused[0]["payload"]["terminal_reason"],
                "structured_output_retry_exhausted",
            )
            self.assertEqual(
                refused[0]["payload"]["subtype"],
                "error_max_structured_output_retries",
            )
            retried = _journaled_events(
                journal, "structured_output_exhaustion_retry_dispatched"
            )
            self.assertEqual(len(retried), 1, retried)
            self.assertEqual(retried[0]["payload"]["attempt"], 2)

    def test_exhaustion_twice_fails(self) -> None:
        with tempfile.TemporaryDirectory() as audit_root:
            journal = _open_audit_journal(Path(audit_root))
            result, prompts = self._run_executor(["exhausted"], audit=journal)
            self.assertEqual(result.status, "failed", result.payload)
            self.assertEqual(
                result.payload.get("error_type"), "StructuredOutputExhaustionError"
            )
            self.assertIn(
                "Claude exited with status 1", result.payload.get("error", "")
            )
            self.assertEqual(len(prompts), 2, prompts)
            refused = _journaled_events(
                journal, "structured_output_exhaustion_refused"
            )
            self.assertEqual(len(refused), 2, refused)
            retried = _journaled_events(
                journal, "structured_output_exhaustion_retry_dispatched"
            )
            self.assertEqual(len(retried), 1, retried)

    def test_unrecognized_nonzero_exit_is_unaffected(self) -> None:
        """A CLI failure that is not this class keeps the pre-existing behavior."""

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
                    argv, 1, stdout="", stderr="boom: unrelated crash"
                )

            executor = ClaudeSemanticTaskExecutor(
                _task(), repository, evidence, "Inspect precisely."
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
                    return_value=_snapshot(),
                ),
            ):
                result = executor.execute(attempt)
            self.assertEqual(result.status, "failed", result.payload)
            self.assertEqual(result.payload.get("error_type"), "LiveExecutionError")
            self.assertIn("boom: unrelated crash", result.payload.get("error", ""))


if __name__ == "__main__":
    unittest.main()
