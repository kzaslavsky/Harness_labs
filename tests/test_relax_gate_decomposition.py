"""Red/green finding test for CB2-06: per-gate verification decomposition.

Item 8 (second half), the decomposed-verification-contract half left open by
CB-03's classification-only landing: a node's deterministic verification can
now be declared as an ordered tuple of named gates, each with its own argv
and timeout, instead of a single flat ``verification_argv`` that serializes
everything into one all-or-nothing command. This file is self-contained
(duplicates the small scaffolds other CB2 finding tests use) so the gate can
run it in isolation.

Behavioral red on the frozen base harness, one method per defect class:

* ``canonical_plan_graph_payload`` (``plan_graph_contract.py``) rejects any
  run declaring a ``verification_gates`` key — its exact-key-set contract has
  no such key at base, so ``PlanGraphContractError`` propagates uncaught.
* ``gate_digest`` (``plan_graph_budget.py``) accepts only a single
  positional ``argv`` at base; passing a ``gates=`` shape raises
  ``TypeError`` for an unexpected keyword argument, so a gate-tuple identity
  cannot be computed or bound to the retry-budget ledger at all.
* ``run_feature_worktree`` (``feature_run.py``) accepts no
  ``verification_gates`` keyword at base, so per-gate command execution,
  classification, and evidence — and the delta-scoped repair renewal and
  infra-transient resume this file exercises around it — are unreachable;
  the same ``TypeError`` propagates uncaught.

On the candidate, every scenario below passes: the canonical contract
accepts an optional ``verification_gates`` tuple while staying byte-identical
in its absence; ``gate_digest`` becomes a total function of the declared
shape; and ``run_feature_worktree`` runs, classifies, and repairs each named
gate independently while restarting the full tuple after any tree-mutating
repair and resuming only the failed gate for a tree-preserving infra retry.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness_labs.core.attempts import TaskResult
from harness_labs.core.controller_kernel import RunContract
from harness_labs.core.controller_results import semantic_payload
from harness_labs.core.controller_scheduler import RoleProfile
from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment
from harness_labs.featurerun.feature_run import run_feature_worktree
from harness_labs.plangraph.plan_graph_budget import BudgetError, RetryBudgetLedger, gate_digest
from harness_labs.plangraph.plan_graph_contract import (
    canonical_json,
    canonical_plan_graph_payload,
)
from tests.controller_scenario_fixtures import ScriptedCoordinatorSession


def gate(name: str, argv: tuple[str, ...], timeout_seconds: float) -> SimpleNamespace:
    """Build one duck-typed gate for run_feature_worktree's verification_gates.

    Deliberately not ``harness_labs.featurerun.feature_run.VerificationGate`` — that
    class does not exist at the frozen base commit, and importing it at
    module scope would fail collection with an ImportError instead of
    letting the red phase fail behaviorally on the missing
    ``verification_gates`` keyword, as the program's red/green contract
    requires.
    """
    return SimpleNamespace(name=name, argv=tuple(argv), timeout_seconds=timeout_seconds)


_PLAN_SHA = hashlib.sha256(b"plan\n").hexdigest()

# Hardcoded from the base-commit formula (json.dumps(list(argv),
# separators=(",", ":")) then sha256) so AC-CB206-4's "byte-identical to
# today" claim is checked against a literal, not against gate_digest's own
# (possibly regressed) output.
_EXPECTED_FLAT_GATE_DIGEST = (
    "77caf50a05bf92616d3f6cb8a7b1f2cc97c4dc104dbae8711483fd1e0645d3e4"
)

# Hardcoded from canonical_plan_graph_payload's base-commit output for one
# flat-argv run, so absence of verification_gates is checked against a
# literal byte string rather than a value this same candidate produced.
_EXPECTED_FLAT_CANONICAL_JSON = (
    '{"acceptance_criteria":{"AC-1":"feature works."},"functionality_tests":[],'
    '"plan":"docs/plan.md","plan_sections":{"1":"Build feature.txt. AC-1: feature '
    'works."},"protocol":"plan-graph-plan/1","referenced_artifacts":[],"runs":['
    '{"allowed_paths":["feature.txt"],"criteria":["AC-1"],"depends_on":[],"id":"A",'
    '"objective":"Build feature.txt","path_intents":[{"action":"create","path":'
    '"feature.txt"}],"plan_sections":["1"],"verification_argv":["echo","hi"],'
    '"verification_required_paths":[],"verification_timeout_seconds":30.0}]}'
)


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


class _BuildExecutor:
    def __init__(self, task, worktree, evidence) -> None:
        self.task = task
        self.worktree = worktree
        self.evidence = evidence

    def execute(self, attempt) -> TaskResult:
        (self.worktree / "feature.txt").write_text("built\n", encoding="utf-8")
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built feature.txt\n",
            media_type="text/markdown",
            producer_task_id=self.task["id"],
        )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built.",
                details_schema=self.task["details_schema"],
                details={"paths": ["feature.txt"]},
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


class _MarkerRepairExecutor:
    """Write one marker file so the failing gate's re-run passes."""

    def __init__(self, worktree: Path, marker: str, seen_contexts: list) -> None:
        self.worktree = worktree
        self.marker = marker
        self.seen_contexts = seen_contexts

    def execute(self, attempt) -> TaskResult:
        self.seen_contexts.append(json.loads(attempt.context))
        (self.worktree / self.marker).write_text("repaired\n", encoding="utf-8")
        return TaskResult(
            attempt.attempt_id,
            "succeeded",
            {"summary": f"Wrote {self.marker}."},
        )


class GateDecompositionTests(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        base = root / "base"
        base.mkdir()
        git(base, "init", "-b", "main")
        git(base, "config", "user.name", "Harness Tests")
        git(base, "config", "user.email", "harness@example.invalid")
        (base / "README.md").write_text("base\n", encoding="utf-8")
        git(base, "add", "README.md")
        git(base, "commit", "--no-gpg-sign", "-m", "Base")
        return base

    def _run_gate_tuple_case(self, root: Path, gates, repair_factory, allowed_paths):
        base = self._repo(root)
        schema = CoordinatorDispatchSchema(
            "feature-gate-decomposition-test/1",
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
                run_id="feature-gate-decomposition-run",
                objective="Build a file behind a decomposed gate tuple.",
                phases=("active",),
                criteria=(
                    {
                        "id": "built",
                        "statement": "The file is built and verified.",
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
                                    "objective": "Build feature.txt",
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
        return run_feature_worktree(
            base_repository=base,
            base_branch="main",
            feature_branch="feature/gate-decomposition",
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
                    lambda task: _BuildExecutor(task, candidate, evidence),
                ),
            ),
            allowed_paths=allowed_paths,
            commit_message="Build feature behind a gate tuple",
            merge=False,
            verification_gates=gates,
            verification_repair_executor_factory=lambda attempt: repair_factory(
                worktree, attempt
            ),
            verification_repair_limit=2,
            evidence_classification="component",
        )

    # AC-CB206-1 / AC-CB206-4: the canonical run payload accepts an optional
    # verification_gates key; its absence is byte-identical to today, and
    # gate_digest becomes a total function of the declared shape (flat argv
    # unchanged, distinct gate tuples never collide with each other or with
    # a flat digest).
    #
    # Behavioral red on base: canonical_plan_graph_payload has no
    # verification_gates key in its exact-key contract, so canonicalizing a
    # run that declares one raises PlanGraphContractError uncaught; and
    # gate_digest accepts only a positional argv, so calling it with a
    # gates= shape raises TypeError uncaught.
    def test_canonical_contract_and_gate_digest_are_total_over_declared_shape(self) -> None:
        flat_run = {
            "id": "A",
            "objective": "Build feature.txt",
            "plan_sections": ["1"],
            "criteria": ["AC-1"],
            "depends_on": [],
            "allowed_paths": ["feature.txt"],
            "path_intents": [{"path": "feature.txt", "action": "create"}],
            "verification_argv": ["echo", "hi"],
            "verification_timeout_seconds": 30,
            "verification_required_paths": [],
        }
        payload = {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Build feature.txt. AC-1: feature works."},
            "acceptance_criteria": {"AC-1": "feature works."},
            "runs": [flat_run],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }
        canonical_flat = canonical_plan_graph_payload(payload)
        self.assertNotIn("verification_gates", canonical_flat["runs"][0])
        self.assertEqual(
            canonical_json(canonical_flat), _EXPECTED_FLAT_CANONICAL_JSON
        )

        gated_run = dict(
            flat_run,
            verification_argv=[],
            verification_gates=[
                {"name": "unit", "argv": ["pytest"], "timeout_seconds": 60},
                {"name": "lint", "argv": ["ruff", "check"], "timeout_seconds": 30},
            ],
        )
        gated_payload = dict(payload, runs=[gated_run])
        canonical_gated = canonical_plan_graph_payload(gated_payload)
        gates = canonical_gated["runs"][0]["verification_gates"]
        self.assertEqual([gate["name"] for gate in gates], ["unit", "lint"])
        self.assertEqual(canonical_gated["runs"][0]["verification_argv"], [])

        flat_digest = gate_digest(("echo", "hi"))
        self.assertEqual(flat_digest, _EXPECTED_FLAT_GATE_DIGEST)

        gate_tuple_a = (
            {"name": "unit", "argv": ["pytest"], "timeout_seconds": 60.0},
        )
        gate_tuple_b = (
            {"name": "unit", "argv": ["pytest", "-x"], "timeout_seconds": 60.0},
        )
        digest_a = gate_digest((), gate_tuple_a)
        digest_b = gate_digest((), gate_tuple_b)
        self.assertNotEqual(digest_a, digest_b)
        self.assertNotEqual(digest_a, flat_digest)
        self.assertNotEqual(digest_b, flat_digest)

    # AC-CB206-4 (ledger half): registration, reservation, and the ledger's
    # gate-change authorization all route through gate_digest, so editing a
    # node from a flat argv identity to a gate-tuple identity is rejected
    # without operator relief and accepted once granted, exactly like today's
    # flat-to-flat gate change.
    #
    # Behavioral red on base: gate_digest((), gates=(...)) raises TypeError
    # uncaught (no such parameter exists), so a gate-tuple identity can never
    # reach RetryBudgetLedger.reserve at all.
    def test_ledger_gate_change_authorization_fires_on_a_gate_tuple_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RetryBudgetLedger(Path(tmp), "gate-edit-lineage")
            flat_digest = gate_digest(("echo", "hi"))
            ledger.register(plan_sha256="a" * 64, gates={"node": flat_digest})
            ledger.reserve(node_id="node", gate=flat_digest)

            gate_tuple_digest = gate_digest(
                (), ({"name": "unit", "argv": ["pytest"], "timeout_seconds": 60.0},)
            )
            self.assertNotEqual(flat_digest, gate_tuple_digest)
            with self.assertRaisesRegex(BudgetError, "gate-change"):
                ledger.reserve(node_id="node", gate=gate_tuple_digest)

            ledger.reset(
                node_id="node",
                reason="operator reviewed the gate-tuple edit",
                accept_gate_change=True,
            )
            ledger.register(plan_sha256="a" * 64, gates={"node": gate_tuple_digest})
            # No BudgetError: the authorized edit is now the bound identity.
            ledger.reserve(node_id="node", gate=gate_tuple_digest)

    # AC-CB206-2 / AC-CB206-3: two named gates run in order, each with its
    # own command attempt, classification, and evidence; a product failure on
    # the second gate dispatches a repair scoped to that gate, and because a
    # repair mutates the tree, the re-verification restarts at the first
    # gate rather than resuming mid-tuple.
    #
    # Behavioral red on base: run_feature_worktree has no verification_gates
    # keyword, so passing one raises TypeError uncaught — per-gate execution,
    # classification, and repair-scoped renewal are unreachable.
    def test_failing_gate_repairs_and_full_tuple_reruns_from_the_first_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts: list = []
            gates = (
                gate(
                    name="unit",
                    argv=(
                        "python3",
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('feature.txt').read_text() == 'built\\n'",
                    ),
                    timeout_seconds=30,
                ),
                gate(
                    name="lint",
                    argv=(
                        "python3",
                        "-c",
                        "import pathlib, sys; "
                        "sys.exit(0 if pathlib.Path('verified.txt').exists() else 7)",
                    ),
                    timeout_seconds=30,
                ),
            )
            result = self._run_gate_tuple_case(
                root,
                gates,
                lambda worktree, attempt: _MarkerRepairExecutor(
                    worktree, "verified.txt", seen_contexts
                ),
                ("feature.txt", "verified.txt"),
            )

            self.assertEqual(
                result.status,
                "succeeded",
                result.review_fix.reason if result.review_fix else result.dispatch.result.payload,
            )
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(result.verification.repair_attempts, 1)
            attempts = result.verification.command_attempts
            self.assertEqual(
                [(item["gate"], item["exit_code"]) for item in attempts],
                [("unit", 0), ("lint", 7), ("unit", 0), ("lint", 0)],
                "the failing 'lint' gate must trigger a repair scoped to it, "
                "and the re-verification must restart at the first gate "
                "('unit') rather than resuming at 'lint'",
            )
            self.assertEqual(seen_contexts[0]["gate"], "lint")
            self.assertEqual(
                seen_contexts[0]["failed_verification"]["gate"], "lint"
            )

    # AC-CB206-2 (infra half) / AC-CB206-3 (infra-resume half): an
    # infrastructure_transient failure on one gate resumes only that gate —
    # no tree mutation, no repair budget charged — and never voids an
    # earlier gate's passing evidence.
    #
    # Behavioral red on base: same TypeError as above (no verification_gates
    # keyword), so infra-transient resume-at-gate semantics are unreachable.
    def test_infra_transient_gate_resumes_without_voiding_earlier_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def repair_factory(worktree, attempt):
                raise AssertionError(
                    "repair must not run for an infrastructure_transient failure"
                )

            gates = (
                gate(
                    name="unit",
                    argv=(
                        "python3",
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('feature.txt').read_text() == 'built\\n'",
                    ),
                    timeout_seconds=30,
                ),
                gate(
                    name="slow-lint",
                    argv=(
                        "python3",
                        "-c",
                        "import pathlib, sys, time\n"
                        "marker = pathlib.Path('slow_lint_marker.txt')\n"
                        "if not marker.exists():\n"
                        "    marker.write_text('seen')\n"
                        "    time.sleep(5)\n"
                        "sys.exit(0)\n",
                    ),
                    timeout_seconds=0.2,
                ),
            )
            result = self._run_gate_tuple_case(
                root, gates, repair_factory, ("feature.txt", "slow_lint_marker.txt")
            )

            self.assertEqual(
                result.status,
                "succeeded",
                result.review_fix.reason if result.review_fix else result.dispatch.result.payload,
            )
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(result.verification.repair_attempts, 0)
            attempts = result.verification.command_attempts
            self.assertEqual(
                [item["gate"] for item in attempts],
                ["unit", "slow-lint", "slow-lint"],
                "the timed-out 'slow-lint' gate must resume in place; the "
                "already-passing 'unit' gate's single attempt must not be "
                "re-run or voided",
            )
            self.assertTrue(attempts[1]["timed_out"])
            self.assertEqual(attempts[1]["failure"]["classification"], "infrastructure_transient")
            self.assertEqual(attempts[2]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
