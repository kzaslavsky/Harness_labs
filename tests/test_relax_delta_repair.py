"""Finding tests for CB-04: delta-scoped verification repair budget.

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it imports nothing that does not already exist at the
base commit (``run_feature_worktree`` and friends), and every assertion is a
controlled ``assert*``/``self.fail`` so a base-harness rejection surfaces as a
pytest FAILED, never an ERROR. The base harness's ``_verify_with_recovery``
charges every repair dispatch against the declared ``verification_repair_limit``
uniformly, so a two-step convergence that needs two repair dispatches within a
declared limit of one blocks there; the candidate's delta-scoping renews the
allowance when a repair strictly shrinks the observed failing set, so the same
scenario completes.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditJournal
from harness_labs.core.controller_kernel import RunContract
from harness_labs.core.controller_results import semantic_payload
from harness_labs.core.controller_scheduler import RoleProfile
from harness_labs.feature_run import run_feature_worktree
from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment
from tests.controller_scenario_fixtures import ScriptedCoordinatorSession


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


_VERIFY_SCRIPT = (
    "import json, pathlib, sys\n"
    "p = pathlib.Path('failing.json')\n"
    "ids = json.loads(p.read_text()) if p.exists() else []\n"
    "for i in ids:\n"
    "    print(f'FAILED tests/test_{i}.py::test_{i} - AssertionError: still failing')\n"
    "sys.exit(1 if ids else 0)\n"
)

# Same declared-failing-test protocol, except the sentinel value "__garbled__"
# produces a failing, non-empty command result whose output contains no
# `FAILED <id>` / `ERROR <id>` line -- an unparseable, non-comparable failing
# set rather than an empty or shrunk one.
_VERIFY_SCRIPT_WITH_GARBLED_OUTPUT = (
    "import json, pathlib, sys\n"
    "p = pathlib.Path('failing.json')\n"
    "ids = json.loads(p.read_text()) if p.exists() else []\n"
    "if ids == ['__garbled__']:\n"
    "    print('AssertionError: verification tool crashed rendering the failing-test summary')\n"
    "    sys.exit(1)\n"
    "for i in ids:\n"
    "    print(f'FAILED tests/test_{i}.py::test_{i} - AssertionError: still failing')\n"
    "sys.exit(1 if ids else 0)\n"
)


class _BuildExecutor:
    """Writes the initial declared-failing-test marker for the verification command."""

    def __init__(self, worktree: Path, evidence, initial_failing: list) -> None:
        self.worktree = worktree
        self.evidence = evidence
        self.initial_failing = initial_failing

    def execute(self, attempt) -> TaskResult:
        (self.worktree / "failing.json").write_text(
            json.dumps(self.initial_failing), encoding="utf-8"
        )
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built candidate with a declared failing-test set.\n",
            media_type="text/markdown",
            producer_task_id=attempt.task_ref.removeprefix("task:"),
        )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built.",
                details_schema="build/1",
                details={"paths": ["failing.json"]},
                artifacts=(artifact.as_dict(),),
                criterion_coverage=(
                    {
                        "criterion_id": "built",
                        "status": "satisfied",
                        "evidence_refs": [artifact.ref],
                    },
                ),
            ),
            evidence=(artifact.ref,),
        )


class _SequencedRepairExecutor:
    """Rewrites the declared-failing-test marker to the next scripted set on each call."""

    def __init__(self, worktree: Path, sequence: list) -> None:
        self.worktree = worktree
        self.sequence = list(sequence)
        self.calls = 0

    def execute(self, attempt) -> TaskResult:
        next_failing = self.sequence[self.calls]
        self.calls += 1
        (self.worktree / "failing.json").write_text(
            json.dumps(next_failing), encoding="utf-8"
        )
        return TaskResult(
            attempt.attempt_id,
            "succeeded",
            {"summary": "Applied a scripted verification repair."},
        )


class RelaxDeltaRepairTests(unittest.TestCase):
    def _run_case(
        self,
        root: Path,
        *,
        initial_failing: list,
        repair_sequence: list,
        repair_limit: int,
        verify_script: str = _VERIFY_SCRIPT,
    ):
        base = root / "base"
        base.mkdir()
        git(base, "init", "-b", "main")
        git(base, "config", "user.name", "Harness Tests")
        git(base, "config", "user.email", "harness@example.invalid")
        (base / "README.md").write_text("base\n", encoding="utf-8")
        git(base, "add", "README.md")
        git(base, "commit", "--no-gpg-sign", "-m", "Base")

        schema = CoordinatorDispatchSchema(
            "delta-repair-test/1",
            (
                CoordinatorSegment(
                    id="active",
                    phases=("active",),
                    instructions="Build and complete.",
                ),
            ),
        )

        def contract_factory(worktree, receipt):
            return RunContract(
                run_id="delta-repair-run",
                objective="Build and verify a file with a shrinking failing set.",
                phases=("active",),
                criteria=(
                    {
                        "id": "built",
                        "statement": "The declared failing set converges to empty.",
                        "source": "operator",
                    },
                ),
                terminal_artifact_kinds=("implementation-summary",),
                repository={
                    "path": str(worktree),
                    "branch": receipt["feature_branch"],
                    "base_branch": receipt["base_branch"],
                    "base_commit": receipt["base_commit"],
                },
            )

        def session_factory(worktree, launch, evidence):
            return ScriptedCoordinatorSession(
                [
                    (
                        "task_dispatch",
                        {
                            "tasks": [
                                {
                                    "id": "build",
                                    "role": "builder",
                                    "objective": "Build failing.json",
                                    "details_schema": "build/1",
                                    "required_capabilities": ["repo.write"],
                                    "acceptance_criteria": ["built"],
                                    "dependencies": [],
                                }
                            ],
                            "max_parallelism": 1,
                        },
                    ),
                    ("run_complete_request", {}),
                ],
                final="Complete.",
            )

        worktree = root / "feature"
        repair_executor = _SequencedRepairExecutor(worktree, repair_sequence)
        result = run_feature_worktree(
            base_repository=base,
            base_branch="main",
            feature_branch="feature/test",
            worktree_path=worktree,
            run_dir=root / "run",
            contract_factory=contract_factory,
            schema=schema,
            session_factory=session_factory,
            profile_builder=lambda candidate, evidence: (
                RoleProfile(
                    "builder",
                    "builder",
                    frozenset({"repo.write"}),
                    lambda task: _BuildExecutor(candidate, evidence, initial_failing),
                ),
            ),
            allowed_paths=("failing.json",),
            commit_message="Build with shrinking failing set",
            merge=False,
            verification_argv=("python3", "-c", verify_script),
            verification_repair_executor_factory=lambda attempt: repair_executor,
            verification_repair_limit=repair_limit,
        )
        return result, repair_executor

    def _events(self, root: Path) -> list:
        return [
            json.loads(line)
            for line in (root / "run" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def test_two_step_convergence_completes_within_declared_repair_limit_of_one(
        self,
    ) -> None:
        # Declared repair limit is one. The first repair only shrinks the
        # failing set from two identifiers to one (renews the allowance on
        # the candidate); the second repair clears the remainder and
        # consumes the sole declared unit. The base harness charges every
        # dispatch unconditionally, so it exhausts the limit after the first
        # repair and blocks instead of converging.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, repair_executor = self._run_case(
                root,
                initial_failing=["alpha", "beta"],
                repair_sequence=[["beta"], []],
                repair_limit=1,
            )

            self.assertEqual(
                result.status,
                "succeeded",
                (
                    result.verification.reason
                    if result.verification
                    else result.dispatch.result.payload
                ),
            )
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.status, "succeeded")
            # repair_attempts is the dispatch count (matches
            # repair_invocation_ids/repair_executor.calls); it is the
            # separately-tracked repair *allowance* that renews, not this
            # attempt record.
            self.assertEqual(result.verification.repair_attempts, 2)
            self.assertEqual(repair_executor.calls, 2)
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [1, 1, 0],
            )
            AuditJournal.verify(root / "run")

    def test_non_improving_repair_consumes_the_declared_limit(self) -> None:
        # A repair that leaves the failing set the same size does not earn a
        # renewal; it consumes the sole declared unit, and the next failure
        # blocks.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, repair_executor = self._run_case(
                root,
                initial_failing=["alpha"],
                repair_sequence=[["alpha"]],
                repair_limit=1,
            )

            self.assertEqual(result.status, "blocked")
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.status, "blocked")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(repair_executor.calls, 1)
            AuditJournal.verify(root / "run")

    def test_larger_failing_set_after_repair_consumes_the_declared_limit(self) -> None:
        # A repair whose rerun grows the failing set is non-improving by the
        # same rule; it must consume the limit rather than renew it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, repair_executor = self._run_case(
                root,
                initial_failing=["alpha"],
                repair_sequence=[["alpha", "beta"]],
                repair_limit=1,
            )

            self.assertEqual(result.status, "blocked")
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.status, "blocked")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(repair_executor.calls, 1)
            AuditJournal.verify(root / "run")

    def test_non_comparable_failing_set_after_repair_consumes_the_declared_limit(
        self,
    ) -> None:
        # The repair's rerun produces a failing, non-empty command result that
        # cannot be parsed into a stable failing-identifier set at all (no
        # `FAILED <id>` / `ERROR <id>` line). A non-comparable set is neither
        # a strict shrink nor provably not one, so it must be treated the same
        # as an equal or larger set: it consumes the declared unit rather than
        # renewing it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, repair_executor = self._run_case(
                root,
                initial_failing=["alpha"],
                repair_sequence=[["__garbled__"]],
                repair_limit=1,
                verify_script=_VERIFY_SCRIPT_WITH_GARBLED_OUTPUT,
            )

            self.assertEqual(result.status, "blocked")
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.status, "blocked")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(repair_executor.calls, 1)

            events = self._events(root)
            delta_events = [
                event
                for event in events
                if event.get("event_type")
                == "deterministic_verification_repair_budget_delta"
            ]
            self.assertEqual(
                len(delta_events),
                1,
                "expected exactly one consumption decision comparing the two failing sets",
            )
            consumption = delta_events[0]
            self.assertEqual(consumption["status"], "consumed")
            self.assertFalse(consumption["payload"]["renewed"])
            self.assertEqual(
                consumption["payload"]["previous_failing_ids"],
                ["tests/test_alpha.py::test_alpha"],
            )
            self.assertIsNone(consumption["payload"]["current_failing_ids"])
            AuditJournal.verify(root / "run")

    def test_hard_iteration_bound_still_holds_despite_repeated_renewal(self) -> None:
        # Every repair in this sequence strictly shrinks the failing set by
        # exactly one, so every one of them would renew the declared limit of
        # one under delta-scoping. The loop's own hard bound on total command
        # executions is untouched by delta-scoping, so an ever-renewing chain
        # that needs more rounds than that bound allows still cannot spin
        # forever: the loop terminates at its fixed iteration bound with a
        # normal `blocked` outcome (never an unhandled exception), the same
        # terminal contract every other exhaustion path in the loop honors.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            declining = [["a", "b", "c", "d", "e", "f", "g"][i:] for i in range(1, 8)]
            result, repair_executor = self._run_case(
                root,
                initial_failing=["a", "b", "c", "d", "e", "f", "g"],
                repair_sequence=declining,
                repair_limit=1,
            )

            self.assertEqual(result.status, "blocked")
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.status, "blocked")
            self.assertEqual(repair_executor.calls, 4)
            AuditJournal.verify(root / "run")

    def test_renewal_and_consumption_decisions_are_recorded_with_compared_sets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._run_case(
                root,
                initial_failing=["alpha", "beta"],
                repair_sequence=[["beta"], []],
                repair_limit=1,
            )
            events = self._events(root)
            delta_events = [
                event
                for event in events
                if event.get("event_type")
                == "deterministic_verification_repair_budget_delta"
            ]
            self.assertEqual(
                len(delta_events),
                1,
                "expected exactly one renewal decision comparing the two failing sets",
            )
            renewal = delta_events[0]
            self.assertEqual(renewal["status"], "renewed")
            self.assertTrue(renewal["payload"]["renewed"])
            self.assertEqual(
                renewal["payload"]["previous_failing_ids"],
                sorted(
                    (
                        "tests/test_alpha.py::test_alpha",
                        "tests/test_beta.py::test_beta",
                    )
                ),
            )
            self.assertEqual(
                renewal["payload"]["current_failing_ids"],
                ["tests/test_beta.py::test_beta"],
            )
            AuditJournal.verify(root / "run")


if __name__ == "__main__":
    unittest.main()
