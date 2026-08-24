"""Tests for capability matching, repeated roles, and bounded delegation."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from harness_labs.core.attempts import TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_commands import CommandActor, CommandEnvelope
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, RunContract, RunLimits
from harness_labs.core.controller_results import semantic_payload
from harness_labs.core.controller_scheduler import (
    CapabilityScheduler,
    RoleProfile,
    SchedulingError,
)
from harness_labs.core.git_transaction import workspace_snapshot


class ResultExecutor:
    def __init__(
        self,
        *,
        details_schema: str,
        payload_factory,
        delay: float = 0,
    ) -> None:
        self.details_schema = details_schema
        self.payload_factory = payload_factory
        self.delay = delay
        self.closed = False

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        if self.delay:
            time.sleep(self.delay)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=self.payload_factory(attempt),
        )

    def close(self) -> None:
        self.closed = True


class ControllerSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceCatalog()
        self.kernel = ControllerKernel(
            RunContract(
                run_id="schedule",
                objective="Inspect flexibly",
                phases=("active",),
                limits=RunLimits(
                    max_depth=3,
                    max_subagents=6,
                    max_parallelism=3,
                    max_tasks=12,
                ),
            ),
            evidence=self.evidence,
        )

    def dispatch_command(self, tasks: list[dict], *, key: str) -> tuple[str, ...]:
        receipt = self.kernel.handle(
            CommandEnvelope(
                command_id=key,
                run_id="schedule",
                type="task.dispatch",
                actor=CommandActor("coordinator", "run_coordinator"),
                expected_revision=self.kernel.revision,
                idempotency_key=key,
                payload={"tasks": tasks, "max_parallelism": 3},
            )
        )
        self.assertTrue(receipt.accepted, receipt.message)
        return tuple(ref.removeprefix("task:") for ref in receipt.effect_refs)

    def test_repeated_roles_get_fresh_parallel_executors(self) -> None:
        created = []

        def factory(task):
            executor = ResultExecutor(
                details_schema=task["details_schema"],
                delay=0.04,
                payload_factory=lambda attempt: semantic_payload(
                    summary=f"Inspected {attempt.attempt_id}",
                    details_schema=task["details_schema"],
                    details={"route": task["id"]},
                ),
            )
            created.append(executor)
            return executor

        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "browser-inspector",
                    "ui_inspector",
                    frozenset({"repo.read", "browser.inspect"}),
                    factory,
                    backend_id="fixture",
                ),
            )
        )
        task_ids = self.dispatch_command(
            [
                {
                    "id": f"inspect-{index}",
                    "role": "ui_inspector",
                    "objective": f"Inspect viewport {index}",
                    "details_schema": "visual-inspection-details/1",
                    "required_capabilities": [
                        "repo.read",
                        "browser.inspect",
                    ],
                    "acceptance_criteria": [],
                    "dependencies": [],
                }
                for index in range(3)
            ],
            key="inspect-batch",
        )

        outcomes = scheduler.dispatch(
            self.kernel,
            task_ids,
            max_parallelism=3,
        )

        self.assertEqual(len(outcomes), 3)
        self.assertEqual(len({id(executor) for executor in created}), 3)
        self.assertGreaterEqual(scheduler.maximum_active, 2)
        self.assertTrue(all(executor.closed for executor in created))

    def test_missing_capability_fails_before_any_task_starts(self) -> None:
        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "code-only",
                    "ui_inspector",
                    frozenset({"repo.read"}),
                    lambda task: ResultExecutor(
                        details_schema=task["details_schema"],
                        payload_factory=lambda attempt: semantic_payload(
                            summary="Code only",
                            details_schema=task["details_schema"],
                            details={},
                        ),
                    ),
                ),
            )
        )
        task_ids = self.dispatch_command(
            [
                {
                    "id": "visual",
                    "role": "ui_inspector",
                    "objective": "Inspect rendered UI",
                    "details_schema": "visual-inspection-details/1",
                    "required_capabilities": ["browser.inspect"],
                    "acceptance_criteria": [],
                    "dependencies": [],
                }
            ],
            key="visual-batch",
        )

        with self.assertRaisesRegex(SchedulingError, "no profile"):
            scheduler.dispatch(self.kernel, task_ids, max_parallelism=1)

        self.assertEqual(self.kernel.task("visual")["status"], "ready")

    def test_playwright_ui_task_cannot_use_non_ui_profile(self) -> None:
        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "code-only",
                    "ui_inspector",
                    frozenset({"repo.read"}),
                    lambda task: ResultExecutor(
                        details_schema=task["details_schema"],
                        payload_factory=lambda attempt: semantic_payload(
                            summary="Code only",
                            details_schema=task["details_schema"],
                            details={},
                        ),
                    ),
                ),
            )
        )
        task_ids = self.dispatch_command(
            [
                {
                    "id": "playwright-visual",
                    "role": "ui_inspector",
                    "objective": "Inspect rendered UI",
                    "details_schema": "visual-inspection-details/1",
                    "required_capabilities": [
                        "repo.read",
                        "browser.playwright.local",
                    ],
                    "acceptance_criteria": [],
                    "dependencies": [],
                }
            ],
            key="playwright-visual-batch",
        )
        with self.assertRaisesRegex(SchedulingError, "no profile"):
            scheduler.dispatch(self.kernel, task_ids, max_parallelism=1)
        self.assertEqual(self.kernel.task("playwright-visual")["status"], "ready")

    def test_profile_view_surfaces_dirty_baseline_eligibility(self) -> None:
        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "eligible",
                    "ui_inspector",
                    frozenset({"repo.write"}),
                    lambda task: None,
                    allow_dirty_baseline=True,
                ),
                RoleProfile(
                    "ineligible",
                    "ui_inspector",
                    frozenset({"repo.write"}),
                    lambda task: None,
                ),
            )
        )
        view = {entry["profile_id"]: entry for entry in scheduler.profile_view}
        self.assertTrue(view["eligible"]["allow_dirty_baseline"])
        self.assertFalse(view["ineligible"]["allow_dirty_baseline"])

    def test_semantic_result_can_request_bounded_subchild(self) -> None:
        def lead_factory(task):
            return ResultExecutor(
                details_schema=task["details_schema"],
                payload_factory=lambda attempt: semantic_payload(
                    summary="Delegated one specialist appraisal.",
                    details_schema=task["details_schema"],
                    details={},
                    delegation_requests=(
                        {
                            "tasks": [
                                {
                                    "id": "architecture-subchild",
                                    "parent_task_id": "architecture-lead",
                                    "role": "architecture_specialist",
                                    "objective": "Inspect module boundaries",
                                    "details_schema": "repository-appraisal-details/1",
                                    "required_capabilities": ["repo.read"],
                                    "acceptance_criteria": [],
                                    "dependencies": [],
                                }
                            ],
                            "max_parallelism": 1,
                        },
                    ),
                ),
            )

        def specialist_factory(task):
            return ResultExecutor(
                details_schema=task["details_schema"],
                payload_factory=lambda attempt: semantic_payload(
                    summary="Inspected architecture.",
                    details_schema=task["details_schema"],
                    details={"layers": ["ui", "domain"]},
                ),
            )

        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "lead",
                    "architecture_lead",
                    frozenset({"repo.read"}),
                    lead_factory,
                ),
                RoleProfile(
                    "specialist",
                    "architecture_specialist",
                    frozenset({"repo.read"}),
                    specialist_factory,
                ),
            )
        )
        parent_ids = self.dispatch_command(
            [
                {
                    "id": "architecture-lead",
                    "role": "architecture_lead",
                    "objective": "Lead architecture appraisal",
                    "details_schema": "repository-appraisal-details/1",
                    "required_capabilities": ["repo.read"],
                    "acceptance_criteria": [],
                    "dependencies": [],
                    "may_delegate": True,
                }
            ],
            key="lead-batch",
        )

        scheduler.dispatch(self.kernel, parent_ids, max_parallelism=1)

        child = self.kernel.task("architecture-subchild")
        self.assertEqual(child["parent_task_id"], "architecture-lead")
        self.assertEqual(child["depth"], 2)
        self.assertEqual(child["status"], "succeeded")


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


class _AuditlessWritableStubExecutor:
    """A hand-wired writable executor that exposes no ``audit`` attribute.

    Models a launcher's executor that never learns about the run's audit
    journal at all -- the scheduler is the only possible source of a
    journaled record for the grant it receives.
    """

    def __init__(self, repository, evidence) -> None:
        self.sandbox = "workspace-write"
        self.repository = repository
        self.evidence = evidence
        self.dirty_baseline_grant = None

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        if self.dirty_baseline_grant is None:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": "writable worker requires a clean repository baseline"},
            )
        (self.repository / "followup.txt").write_text("done\n", encoding="utf-8")
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Follow-up complete.", details_schema="stub/1", details={}
            ),
        )


class _WritableStubExecutor:
    """A hand-wired writable executor: never computes its own grant.

    Mirrors a launcher that constructs its executor directly (not through
    ``agent_mixture``), so the only source of a grant is whatever the
    dispatch chokepoint attaches to ``dirty_baseline_grant`` before
    ``execute`` runs.
    """

    def __init__(self, repository, evidence, *, audit=None) -> None:
        self.sandbox = "workspace-write"
        self.repository = repository
        self.evidence = evidence
        self.audit = audit
        self.dirty_baseline_grant = None

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        if self.dirty_baseline_grant is None:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": "writable worker requires a clean repository baseline"},
            )
        (self.repository / "followup.txt").write_text("done\n", encoding="utf-8")
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Follow-up complete.", details_schema="stub/1", details={}
            ),
        )


class DirtyBaselineDispatchChokepointTests(unittest.TestCase):
    """AC-CB303-1..3: grants minted at ``CapabilityScheduler.dispatch`` itself.

    Every executor here is hand-wired (a plain stub, not built through
    ``agent_mixture``), so any grant these tasks receive can only have come
    from the dispatch chokepoint -- proving the mechanism now reaches every
    program using the controller path, not only agent_mixture-built ones.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "repo"
        self.repository.mkdir()
        _git(self.repository, "init", "-b", "main")
        _git(self.repository, "config", "user.name", "Harness Tests")
        _git(self.repository, "config", "user.email", "harness@example.invalid")
        (self.repository / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.repository, "add", "README.md")
        _git(self.repository, "commit", "--no-gpg-sign", "-m", "Base")
        self.evidence = EvidenceCatalog()
        self.kernel = ControllerKernel(
            RunContract(
                run_id="dispatch-chokepoint",
                objective="Exercise the dispatch-chokepoint grant.",
                phases=("active",),
                limits=RunLimits(
                    max_depth=3,
                    max_subagents=6,
                    max_parallelism=3,
                    max_tasks=12,
                ),
            ),
            evidence=self.evidence,
        )

    def dispatch_command(self, tasks: list[dict], *, key: str) -> tuple[str, ...]:
        receipt = self.kernel.handle(
            CommandEnvelope(
                command_id=key,
                run_id="dispatch-chokepoint",
                type="task.dispatch",
                actor=CommandActor("coordinator", "run_coordinator"),
                expected_revision=self.kernel.revision,
                idempotency_key=key,
                payload={"tasks": tasks, "max_parallelism": 1},
            )
        )
        self.assertTrue(receipt.accepted, receipt.message)
        return tuple(ref.removeprefix("task:") for ref in receipt.effect_refs)

    def _receipt(self, changed_paths: list[str]):
        snapshot = workspace_snapshot(self.repository)
        return self.evidence.add(
            kind="workspace-change-receipt",
            content={
                "protocol": "workspace-change-receipt/2",
                "changed_paths": list(changed_paths),
                "files": {
                    path: snapshot["files"].get(path) for path in changed_paths
                },
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )

    def _dispatch_one_followup(
        self, profile: RoleProfile, *, audit: AuditJournal | None = None
    ):
        task_ids = self.dispatch_command(
            [
                {
                    "id": "followup",
                    "role": "followup",
                    "objective": "Follow up",
                    "details_schema": "stub/1",
                    "required_capabilities": ["repo.write"],
                    "acceptance_criteria": [],
                    "dependencies": [],
                }
            ],
            key="followup-batch",
        )
        scheduler = CapabilityScheduler((profile,), audit=audit)
        return scheduler.dispatch(self.kernel, task_ids, max_parallelism=1), task_ids

    def test_covering_receipt_mints_a_grant_and_journals_the_receipt_ref(
        self,
    ) -> None:
        (self.repository / "feature.txt").write_text("built\n", encoding="utf-8")
        receipt = self._receipt(["feature.txt"])

        run_dir = Path(self.temporary.name) / "run"
        audit = AuditJournal(
            run_dir, "dispatch-chokepoint", actor=AuditActor("test", "controller")
        )
        stub = _WritableStubExecutor(self.repository, self.evidence, audit=audit)
        profile = RoleProfile(
            "followup",
            "followup",
            frozenset({"repo.write"}),
            lambda task: stub,
            allow_dirty_baseline=True,
        )

        outcomes, task_ids = self._dispatch_one_followup(profile)

        self.assertEqual(outcomes[0].result.status, "succeeded")
        self.assertEqual(stub.dirty_baseline_grant, {"receipt_ref": receipt.ref})

        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        granted = [
            event
            for event in events
            if event["event_type"] == "dirty_baseline_adoption_grant_supplied"
            and event["status"] == "granted"
        ]
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0]["payload"]["receipt_ref"], receipt.ref)
        self.assertEqual(granted[0]["attempt_id"], f"{task_ids[0]}/attempt-1")

    def test_scheduler_audit_journals_the_grant_when_executor_has_none(
        self,
    ) -> None:
        (self.repository / "feature.txt").write_text("built\n", encoding="utf-8")
        receipt = self._receipt(["feature.txt"])

        run_dir = Path(self.temporary.name) / "run"
        audit = AuditJournal(
            run_dir, "dispatch-chokepoint", actor=AuditActor("test", "controller")
        )
        stub = _AuditlessWritableStubExecutor(self.repository, self.evidence)
        profile = RoleProfile(
            "followup",
            "followup",
            frozenset({"repo.write"}),
            lambda task: stub,
            allow_dirty_baseline=True,
        )

        outcomes, task_ids = self._dispatch_one_followup(profile, audit=audit)

        self.assertEqual(outcomes[0].result.status, "succeeded")
        self.assertEqual(stub.dirty_baseline_grant, {"receipt_ref": receipt.ref})

        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        granted = [
            event
            for event in events
            if event["event_type"] == "dirty_baseline_adoption_grant_supplied"
            and event["status"] == "granted"
        ]
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0]["payload"]["receipt_ref"], receipt.ref)
        self.assertEqual(granted[0]["attempt_id"], f"{task_ids[0]}/attempt-1")

    def test_scheduler_audit_is_preferred_over_the_executors_own(self) -> None:
        (self.repository / "feature.txt").write_text("built\n", encoding="utf-8")
        self._receipt(["feature.txt"])

        run_dir = Path(self.temporary.name) / "run"
        scheduler_audit = AuditJournal(
            run_dir / "scheduler",
            "dispatch-chokepoint",
            actor=AuditActor("test", "controller"),
        )
        executor_audit = AuditJournal(
            run_dir / "executor",
            "dispatch-chokepoint",
            actor=AuditActor("test", "controller"),
        )
        stub = _WritableStubExecutor(
            self.repository, self.evidence, audit=executor_audit
        )
        profile = RoleProfile(
            "followup",
            "followup",
            frozenset({"repo.write"}),
            lambda task: stub,
            allow_dirty_baseline=True,
        )

        self._dispatch_one_followup(profile, audit=scheduler_audit)

        def _grant_events(audit: AuditJournal) -> list[dict]:
            return [
                json.loads(line)
                for line in audit.events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if json.loads(line)["event_type"]
                == "dirty_baseline_adoption_grant_supplied"
            ]

        self.assertEqual(len(_grant_events(scheduler_audit)), 1)
        self.assertEqual(len(_grant_events(executor_audit)), 0)

    def test_no_single_receipt_covering_both_dirty_paths_yields_no_grant(
        self,
    ) -> None:
        (self.repository / "a.txt").write_text("a\n", encoding="utf-8")
        (self.repository / "b.txt").write_text("b\n", encoding="utf-8")
        self._receipt(["a.txt"])
        self._receipt(["b.txt"])

        stub = _WritableStubExecutor(self.repository, self.evidence)
        profile = RoleProfile(
            "followup",
            "followup",
            frozenset({"repo.write"}),
            lambda task: stub,
            allow_dirty_baseline=True,
        )

        outcomes, _ = self._dispatch_one_followup(profile)

        self.assertEqual(outcomes[0].result.status, "failed")
        self.assertIsNone(stub.dirty_baseline_grant)

    def test_ineligible_role_gets_no_grant_even_with_a_covering_receipt(
        self,
    ) -> None:
        (self.repository / "feature.txt").write_text("built\n", encoding="utf-8")
        self._receipt(["feature.txt"])

        stub = _WritableStubExecutor(self.repository, self.evidence)
        profile = RoleProfile(
            "followup",
            "followup",
            frozenset({"repo.write"}),
            lambda task: stub,
            allow_dirty_baseline=False,
        )

        outcomes, _ = self._dispatch_one_followup(profile)

        self.assertEqual(outcomes[0].result.status, "failed")
        self.assertIsNone(stub.dirty_baseline_grant)

    def test_a_grant_already_supplied_by_the_factory_is_left_untouched(
        self,
    ) -> None:
        (self.repository / "feature.txt").write_text("built\n", encoding="utf-8")
        self._receipt(["feature.txt"])
        preset = {"receipt_ref": "artifact:sha256:" + "0" * 64}

        stub = _WritableStubExecutor(self.repository, self.evidence)
        stub.dirty_baseline_grant = preset
        profile = RoleProfile(
            "followup",
            "followup",
            frozenset({"repo.write"}),
            lambda task: stub,
            allow_dirty_baseline=True,
        )

        self._dispatch_one_followup(profile)

        self.assertEqual(stub.dirty_baseline_grant, preset)


if __name__ == "__main__":
    unittest.main()
