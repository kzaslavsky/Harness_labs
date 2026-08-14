"""Finding test for CB3-03: dispatch-chokepoint dirty-baseline adoption grants.

Reproduces item 17 of ``docs/development/contract-burden-reduction.md`` (the
CB2-02 root-attempt specimen): a builder task succeeds and leaves a receipted
dirty workspace, then the coordinator's very next writable dispatch in that
same workspace -- a follow-up, built by a launcher that hand-wires its own
executor factory rather than routing through ``agent_mixture`` -- is refused
with "writable worker requires a clean repository baseline" even though a
workspace-change receipt in the run's own evidence catalog exactly covers
the dirty state.

At the frozen base commit, ``CapabilityScheduler.dispatch`` (the controller's
single dispatch chokepoint, used by every controller-path program, coordinator
or stage-machine alike) calls ``profile.executor_factory(task)`` and runs the
resulting executor without ever inspecting the workspace or the evidence
catalog for a covering receipt. Only a launcher that builds its executors
through ``agent_mixture.build_role_profiles`` gets a per-dispatch grant (that
factory computes one itself); a launcher that constructs its own executor
directly -- exactly the shape this test drives -- never receives one, so its
follow-up dispatch is stranded behind the clean-baseline refusal.

The follow-up executor here calls the real base-commit
``CodexSemanticTaskExecutor._resolve_dirty_baseline_grant`` preflight check
directly (the same production entry point CB3-02's own finding test uses)
instead of shelling out to the ``codex`` CLI, so the refusal/success this
test observes is the production preflight's own decision, not a
re-implementation of it.
"""

from __future__ import annotations

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from harness_labs.attempts import TaskAttempt, TaskResult
from harness_labs.controller_commands import CommandActor, CommandEnvelope
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import ControllerKernel, RunContract, RunLimits
from harness_labs.controller_live import CodexSemanticTaskExecutor, LiveExecutionError
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_scheduler import CapabilityScheduler, RoleProfile
from harness_labs.git_transaction import workspace_snapshot


def git(repository: Path, *args: str) -> str:
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


def _role_profile(
    *,
    profile_id: str,
    role: str,
    capabilities: frozenset[str],
    executor_factory,
    eligible: bool,
) -> RoleProfile:
    """Build a ``RoleProfile``, opting into dirty-baseline eligibility only
    when the installed ``RoleProfile`` actually exposes that field.

    At the frozen base commit ``RoleProfile`` carries no eligibility concept
    at all -- the dispatch chokepoint mints nothing for anyone, so omitting
    the field there is the correct, entry-point-honest construction, not a
    workaround. At the candidate the field exists and is passed through, so
    the same call proves the eligibility gate itself (AC-CB303-3) alongside
    the core adoption behavior.
    """

    kwargs: dict[str, Any] = dict(
        profile_id=profile_id,
        role=role,
        capabilities=capabilities,
        executor_factory=executor_factory,
        backend_id="fixture",
    )
    field_names = {field.name for field in dataclasses.fields(RoleProfile)}
    if "allow_dirty_baseline" in field_names:
        kwargs["allow_dirty_baseline"] = eligible
    return RoleProfile(**kwargs)


class _BuildExecutor:
    """A hand-wired writable builder task that succeeds, leaving a receipt.

    Writes ``feature.txt`` and mints a ``workspace-change-receipt`` recording
    the real on-disk content, exactly as the live executors do -- the
    genuine "successful attempt's uncommitted receipted work" the follow-up
    dispatch must then be able to adopt.
    """

    def __init__(self, repository: Path, evidence: EvidenceCatalog) -> None:
        self.repository = repository
        self.evidence = evidence

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        path = self.repository / "feature.txt"
        path.write_text("built\n", encoding="utf-8")
        snapshot = workspace_snapshot(self.repository)
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built the candidate with a declared change.\n",
            media_type="text/markdown",
            producer_task_id="build",
        )
        receipt = self.evidence.add(
            kind="workspace-change-receipt",
            content={
                "protocol": "workspace-change-receipt/2",
                "changed_paths": ["feature.txt"],
                "files": {"feature.txt": snapshot["files"].get("feature.txt")},
            },
            media_type="application/json",
            producer_task_id="build",
        )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built the feature file.",
                details_schema="build/1",
                details={"paths": ["feature.txt"]},
                artifacts=(artifact.as_dict(), receipt.as_dict()),
            ),
            evidence=(artifact.ref, receipt.ref),
        )


class _HandWiredFollowupExecutor:
    """A coordinator-dispatched writable follow-up built without agent_mixture.

    ``dirty_baseline_grant`` starts ``None``; a caller that mints a grant at
    the dispatch chokepoint must mutate it before ``execute`` runs, exactly
    as ``feature_run._attach_dirty_baseline_grant`` already does for the
    review-fix path. This models the launcher named in the CB3-03 objective:
    one that hand-wires its own executor rather than routing through
    ``agent_mixture.build_role_profiles``.
    """

    dirty_baseline_grant: Mapping[str, Any] | None = None

    def __init__(self, repository: Path, evidence: EvidenceCatalog) -> None:
        self.repository = repository
        self.evidence = evidence
        self.sandbox = "workspace-write"

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        executor = CodexSemanticTaskExecutor(
            task={
                "id": "followup",
                "objective": "Follow up on the build",
                "context": "{}",
                "details_schema": "followup-fix/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            },
            repository=self.repository,
            evidence=self.evidence,
            role_instructions="Follow up only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt", "followup.txt"),
            dirty_baseline_grant=self.dirty_baseline_grant,
        )
        initial_workspace = workspace_snapshot(self.repository)
        try:
            executor._resolve_dirty_baseline_grant(initial_workspace)
        except LiveExecutionError as exc:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
        (self.repository / "followup.txt").write_text("followed up\n", encoding="utf-8")
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Follow-up complete.",
                details_schema="followup-fix/1",
                details={"paths": ["followup.txt"]},
            ),
        )


class DispatchChokepointFollowupGrantTest(unittest.TestCase):
    """AC-CB303-4: a coordinator follow-up dispatch adopts the covering receipt."""

    def test_followup_dispatch_after_receipted_success_is_not_stranded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Harness Tests")
            git(repository, "config", "user.email", "harness@example.invalid")
            (repository / "README.md").write_text("base\n", encoding="utf-8")
            git(repository, "add", "README.md")
            git(repository, "commit", "--no-gpg-sign", "-m", "Base")

            evidence = EvidenceCatalog()
            kernel = ControllerKernel(
                RunContract(
                    run_id="followup-grant-run",
                    objective="Build then dispatch a writable follow-up.",
                    phases=("active",),
                    limits=RunLimits(
                        max_depth=3,
                        max_subagents=6,
                        max_parallelism=3,
                        max_tasks=12,
                    ),
                ),
                evidence=evidence,
            )

            def dispatch_command(tasks: list[dict], *, key: str) -> tuple[str, ...]:
                receipt = kernel.handle(
                    CommandEnvelope(
                        command_id=key,
                        run_id="followup-grant-run",
                        type="task.dispatch",
                        actor=CommandActor("coordinator", "run_coordinator"),
                        expected_revision=kernel.revision,
                        idempotency_key=key,
                        payload={"tasks": tasks, "max_parallelism": 1},
                    )
                )
                self.assertTrue(receipt.accepted, receipt.message)
                return tuple(
                    ref.removeprefix("task:") for ref in receipt.effect_refs
                )

            scheduler = CapabilityScheduler(
                (
                    _role_profile(
                        profile_id="builder",
                        role="builder",
                        capabilities=frozenset({"repo.write"}),
                        executor_factory=lambda task: _BuildExecutor(
                            repository, evidence
                        ),
                        eligible=False,
                    ),
                    _role_profile(
                        profile_id="followup",
                        role="followup",
                        capabilities=frozenset({"repo.write"}),
                        executor_factory=lambda task: _HandWiredFollowupExecutor(
                            repository, evidence
                        ),
                        eligible=True,
                    ),
                )
            )

            build_ids = dispatch_command(
                [
                    {
                        "id": "build",
                        "role": "builder",
                        "objective": "Build feature.txt",
                        "details_schema": "build/1",
                        "required_capabilities": ["repo.write"],
                        "acceptance_criteria": [],
                        "dependencies": [],
                    }
                ],
                key="build-batch",
            )
            build_outcomes = scheduler.dispatch(kernel, build_ids, max_parallelism=1)
            self.assertEqual(build_outcomes[0].result.status, "succeeded")

            followup_ids = dispatch_command(
                [
                    {
                        "id": "followup",
                        "role": "followup",
                        "objective": "Address the follow-up in the same workspace",
                        "details_schema": "followup-fix/1",
                        "required_capabilities": ["repo.write"],
                        "acceptance_criteria": [],
                        "dependencies": [],
                    }
                ],
                key="followup-batch",
            )
            followup_outcomes = scheduler.dispatch(
                kernel, followup_ids, max_parallelism=1
            )
            result = followup_outcomes[0].result

            self.assertEqual(
                result.status,
                "succeeded",
                "a coordinator-dispatched writable follow-up in a workspace "
                "whose dirty state is exactly covered by an existing "
                "workspace-change receipt must not be stranded behind the "
                "clean-baseline refusal: "
                + str(result.payload),
            )
            self.assertTrue((repository / "followup.txt").exists())


if __name__ == "__main__":
    unittest.main()
