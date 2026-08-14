"""Tests for the declarative per-role agent mixture layer."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs import (
    ClaudeAgentSession,
    CodexAppServerSession,
    build_coordinator_session,
    BackendSpec,
    ClaudeSemanticTaskExecutor,
    CodexSemanticTaskExecutor,
    WorkerRole,
    build_role_profiles,
    parse_backend_spec,
    resolve_backend_spec,
    task_with_artifact_kind,
)
from harness_labs.core.controller_evidence import EvidenceCatalog


def _role(**overrides) -> WorkerRole:
    values = dict(
        profile_id="builder",
        role="demo_builder",
        capabilities=frozenset({"repo.read", "repo.write"}),
        details_schemas=frozenset({"demo-implementation/1"}),
        instructions="Build the feature.",
        artifact_kind="implementation-summary",
        sandbox="workspace-write",
        writable_paths=("index.html",),
        require_repository_change=True,
    )
    values.update(overrides)
    return WorkerRole(**values)


class BackendSpecTests(unittest.TestCase):
    def test_parses_provider_model_and_effort(self) -> None:
        spec = parse_backend_spec("claude:claude-opus-5@high")
        self.assertEqual(spec.provider, "claude")
        self.assertEqual(spec.model, "claude-opus-5")
        self.assertEqual(spec.effort, "high")
        self.assertEqual(spec.backend_id, "claude-print")

    def test_effort_defaults_to_medium(self) -> None:
        spec = parse_backend_spec("codex:gpt-5.6-terra")
        self.assertEqual(spec.effort, "medium")
        self.assertEqual(spec.backend_id, "codex-exec")

    def test_passes_existing_spec_through(self) -> None:
        spec = BackendSpec("claude", "claude-sonnet-5")
        self.assertIs(parse_backend_spec(spec), spec)

    def test_rejects_unknown_provider_and_malformed_specs(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider"):
            parse_backend_spec("gemini:some-model")
        with self.assertRaisesRegex(ValueError, "provider:model"):
            parse_backend_spec("claude")
        with self.assertRaisesRegex(ValueError, "no model"):
            parse_backend_spec("claude:@high")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            parse_backend_spec("")


class WorkerRoleTests(unittest.TestCase):
    def test_mirrors_executor_sandbox_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "writable_paths"):
            _role(writable_paths=())
        with self.assertRaisesRegex(ValueError, "workspace-write sandbox"):
            _role(
                sandbox="read-only",
                writable_paths=(),
                require_repository_change=True,
            )
        with self.assertRaisesRegex(ValueError, "preflight"):
            _role(preflight_argv=(), require_preflight_success=True)


class MixtureResolutionTests(unittest.TestCase):
    def test_role_name_wins_over_profile_id_and_default(self) -> None:
        role = _role()
        mixture = {
            "demo_builder": "claude:claude-opus-5@high",
            "builder": "codex:gpt-5.6-terra",
            "*": "codex:gpt-5.6-terra@low",
        }
        self.assertEqual(resolve_backend_spec(mixture, role).provider, "claude")
        del mixture["demo_builder"]
        self.assertEqual(
            resolve_backend_spec(mixture, role).model, "gpt-5.6-terra"
        )
        del mixture["builder"]
        self.assertEqual(resolve_backend_spec(mixture, role).effort, "low")

    def test_missing_role_without_default_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "names no backend"):
            resolve_backend_spec({"other": "claude:claude-sonnet-5"}, _role())


class BuildRoleProfilesTests(unittest.TestCase):
    def test_builds_mixed_provider_profiles(self) -> None:
        roles = (
            _role(),
            _role(
                profile_id="verifier",
                role="demo_verifier",
                capabilities=frozenset({"repo.read"}),
                details_schemas=frozenset({"demo-verification/1"}),
                instructions="Verify the feature.",
                artifact_kind="verification-report",
                sandbox="read-only",
                writable_paths=(),
                require_repository_change=False,
                preflight_argv=("python3", "verify.py"),
                require_preflight_success=True,
            ),
        )
        profiles = build_role_profiles(
            mixture={
                "demo_builder": "claude:claude-opus-5@high",
                "*": "codex:gpt-5.6-terra",
            },
            roles=roles,
            repository=Path("."),
            evidence=EvidenceCatalog(),
            executables={"codex": "/apps/codex"},
        )

        self.assertEqual(
            [profile.backend_id for profile in profiles],
            ["claude-print", "codex-exec"],
        )
        task = {
            "id": "build-1",
            "objective": "Build",
            "context": "",
            "details_schema": "demo-implementation/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        builder = profiles[0].executor_factory(task)
        self.assertIsInstance(builder, ClaudeSemanticTaskExecutor)
        self.assertEqual(builder.model, "claude-opus-5")
        self.assertEqual(builder.effort, "high")
        self.assertEqual(builder.executable, "claude")
        self.assertEqual(builder.sandbox, "workspace-write")
        self.assertEqual(builder.writable_paths, ("index.html",))
        self.assertEqual(
            json.loads(builder.task["context"])["artifact_kind"],
            "implementation-summary",
        )

        verifier = profiles[1].executor_factory({**task, "id": "verify-1"})
        self.assertIsInstance(verifier, CodexSemanticTaskExecutor)
        self.assertEqual(verifier.model, "gpt-5.6-terra")
        self.assertEqual(verifier.reasoning, "medium")
        self.assertEqual(verifier.executable, "/apps/codex")
        self.assertEqual(verifier.preflight_argv, ("python3", "verify.py"))
        self.assertTrue(verifier.require_preflight_success)

    def test_propagates_role_allow_dirty_baseline_to_the_profile(self) -> None:
        roles = (
            _role(allow_dirty_baseline=True),
            _role(
                profile_id="verifier",
                role="demo_verifier",
                sandbox="read-only",
                writable_paths=(),
                require_repository_change=False,
            ),
        )
        profiles = build_role_profiles(
            mixture={"*": "claude:claude-opus-5@high"},
            roles=roles,
            repository=Path("."),
            evidence=EvidenceCatalog(),
        )
        self.assertTrue(profiles[0].allow_dirty_baseline)
        self.assertFalse(profiles[1].allow_dirty_baseline)

    def test_rejects_unknown_executable_providers_and_empty_roles(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one worker role"):
            build_role_profiles(
                mixture={"*": "claude:claude-sonnet-5"},
                roles=(),
                repository=Path("."),
                evidence=EvidenceCatalog(),
            )
        with self.assertRaisesRegex(ValueError, "unknown providers"):
            build_role_profiles(
                mixture={"*": "claude:claude-sonnet-5"},
                roles=(_role(),),
                repository=Path("."),
                evidence=EvidenceCatalog(),
                executables={"gemini": "/bin/gemini"},
            )


class TaskArtifactKindTests(unittest.TestCase):
    def test_folds_artifact_kind_into_json_context(self) -> None:
        task = {"id": "t", "context": json.dumps({"cycle": 1})}
        bound = task_with_artifact_kind(task, "review-report")
        context = json.loads(bound["context"])
        self.assertEqual(context["cycle"], 1)
        self.assertEqual(context["artifact_kind"], "review-report")

    def test_preserves_non_json_context(self) -> None:
        bound = task_with_artifact_kind({"id": "t", "context": "free text"}, "kind")
        context = json.loads(bound["context"])
        self.assertEqual(context["supplied_context"], "free text")
        self.assertEqual(context["artifact_kind"], "kind")


class DirtyBaselineGrantTests(unittest.TestCase):
    """AC-CB05-1: agent_mixture supplies the adoption grant, not the coordinator."""

    def test_ineligible_role_gets_no_grant_and_never_inspects_the_workspace(
        self,
    ) -> None:
        with patch(
            "harness_labs.agent_mixture.workspace_snapshot"
        ) as snapshot:
            profiles = build_role_profiles(
                mixture={"*": "claude:claude-opus-5@high"},
                roles=(_role(),),
                repository=Path("."),
                evidence=EvidenceCatalog(),
            )
            executor = profiles[0].executor_factory(
                {
                    "id": "build-1",
                    "objective": "Build",
                    "context": "",
                    "details_schema": "demo-implementation/1",
                    "acceptance_criteria": [],
                    "required_capabilities": ["repo.write"],
                }
            )
        self.assertIsNone(executor.dirty_baseline_grant)
        snapshot.assert_not_called()

    def test_eligible_role_with_a_clean_workspace_gets_no_grant(self) -> None:
        with patch(
            "harness_labs.agent_mixture.workspace_snapshot",
            return_value={"changed_paths": [], "files": {}},
        ):
            profiles = build_role_profiles(
                mixture={"*": "claude:claude-opus-5@high"},
                roles=(_role(allow_dirty_baseline=True),),
                repository=Path("."),
                evidence=EvidenceCatalog(),
            )
            executor = profiles[0].executor_factory(
                {
                    "id": "fix-1",
                    "objective": "Fix",
                    "context": "",
                    "details_schema": "demo-implementation/1",
                    "acceptance_criteria": [],
                    "required_capabilities": ["repo.write"],
                }
            )
        self.assertIsNone(executor.dirty_baseline_grant)

    def test_eligible_role_adopts_the_receipt_that_covers_the_dirty_workspace(
        self,
    ) -> None:
        evidence = EvidenceCatalog()
        receipt = evidence.add(
            kind="workspace-change-receipt",
            content={"changed_paths": ["index.html"]},
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        with patch(
            "harness_labs.agent_mixture.workspace_snapshot",
            return_value={"changed_paths": ["index.html"], "files": {}},
        ):
            profiles = build_role_profiles(
                mixture={"*": "claude:claude-opus-5@high"},
                roles=(_role(allow_dirty_baseline=True),),
                repository=Path("."),
                evidence=evidence,
            )
            executor = profiles[0].executor_factory(
                {
                    "id": "fix-1",
                    "objective": "Fix",
                    "context": "",
                    "details_schema": "demo-implementation/1",
                    "acceptance_criteria": [],
                    "required_capabilities": ["repo.write"],
                }
            )
        self.assertEqual(
            executor.dirty_baseline_grant, {"receipt_ref": receipt.ref}
        )

    def test_eligible_role_prefers_the_tightest_covering_receipt(self) -> None:
        """Regression for the CB3-03 refactor onto the shared selector.

        Selection now delegates to
        ``controller_live.select_dirty_baseline_receipt`` instead of a
        private in-module loop; this pins the tightest-covering-receipt
        preference the delegation must preserve.
        """

        evidence = EvidenceCatalog()
        wide = evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["index.html", "extra.txt"],
                "files": {
                    "index.html": {"kind": "file", "sha256": "same"},
                    "extra.txt": {"kind": "file", "sha256": "same"},
                },
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        tight = evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["index.html"],
                "files": {"index.html": {"kind": "file", "sha256": "same"}},
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        with patch(
            "harness_labs.agent_mixture.workspace_snapshot",
            return_value={
                "changed_paths": ["index.html"],
                "files": {"index.html": {"kind": "file", "sha256": "same"}},
            },
        ):
            profiles = build_role_profiles(
                mixture={"*": "claude:claude-opus-5@high"},
                roles=(_role(allow_dirty_baseline=True),),
                repository=Path("."),
                evidence=evidence,
            )
            executor = profiles[0].executor_factory(
                {
                    "id": "fix-1",
                    "objective": "Fix",
                    "context": "",
                    "details_schema": "demo-implementation/1",
                    "acceptance_criteria": [],
                    "required_capabilities": ["repo.write"],
                }
            )
        self.assertEqual(executor.dirty_baseline_grant, {"receipt_ref": tight.ref})
        self.assertNotEqual(executor.dirty_baseline_grant["receipt_ref"], wide.ref)

    def test_eligible_role_declines_a_receipt_whose_content_has_drifted(
        self,
    ) -> None:
        """Path coverage alone must not be enough: content has to match too.

        The receipt covers ``index.html`` by path, but its recorded content
        state is stale relative to what ``workspace_snapshot`` reports on
        disk right now, so the shared content check must reject it even
        though the earlier coverage-only test for the same setup accepts an
        equivalent receipt.
        """

        evidence = EvidenceCatalog()
        evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["index.html"],
                "files": {"index.html": {"kind": "file", "sha256": "before"}},
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        with patch(
            "harness_labs.agent_mixture.workspace_snapshot",
            return_value={
                "changed_paths": ["index.html"],
                "files": {"index.html": {"kind": "file", "sha256": "after"}},
            },
        ):
            profiles = build_role_profiles(
                mixture={"*": "claude:claude-opus-5@high"},
                roles=(_role(allow_dirty_baseline=True),),
                repository=Path("."),
                evidence=evidence,
            )
            executor = profiles[0].executor_factory(
                {
                    "id": "fix-1",
                    "objective": "Fix",
                    "context": "",
                    "details_schema": "demo-implementation/1",
                    "acceptance_criteria": [],
                    "required_capabilities": ["repo.write"],
                }
            )
        self.assertIsNone(executor.dirty_baseline_grant)

    def test_coordinator_authored_context_cannot_name_its_own_grant(self) -> None:
        """A coordinator-supplied dirty_baseline_grant in task context is ignored.

        Even a task whose context claims a receipt covering unrelated,
        never-receipted paths must not influence the grant the executor
        receives: only the controller's own evidence-catalog lookup can.
        """

        evidence = EvidenceCatalog()
        forged_ref = "artifact:sha256:" + "0" * 64
        with patch(
            "harness_labs.agent_mixture.workspace_snapshot",
            return_value={"changed_paths": ["index.html"], "files": {}},
        ):
            profiles = build_role_profiles(
                mixture={"*": "claude:claude-opus-5@high"},
                roles=(_role(allow_dirty_baseline=True),),
                repository=Path("."),
                evidence=evidence,
            )
            executor = profiles[0].executor_factory(
                {
                    "id": "fix-1",
                    "objective": "Fix",
                    "context": json.dumps(
                        {"dirty_baseline_grant": {"receipt_ref": forged_ref}}
                    ),
                    "details_schema": "demo-implementation/1",
                    "acceptance_criteria": [],
                    "required_capabilities": ["repo.write"],
                }
            )
        self.assertIsNone(executor.dirty_baseline_grant)


class CoordinatorSessionTests(unittest.TestCase):
    def test_claude_spec_builds_a_claude_agent_session(self) -> None:
        session = build_coordinator_session(
            "claude:claude-opus-5@high",
            base_instructions="coordinate",
            timeout_seconds=42.0,
        )
        self.assertIsInstance(session, ClaudeAgentSession)
        self.assertEqual(session.model, "claude-opus-5")
        self.assertEqual(session.effort, "high")
        self.assertEqual(session.executable, "claude")
        self.assertEqual(session.base_instructions, "coordinate")
        self.assertEqual(session.timeout_seconds, 42.0)

    def test_codex_spec_builds_a_codex_app_server_session(self) -> None:
        session = build_coordinator_session("codex:gpt-5.6-terra@low")
        self.assertIsInstance(session, CodexAppServerSession)
        self.assertEqual(session.model, "gpt-5.6-terra")
        self.assertEqual(session.reasoning, "low")
        self.assertEqual(session.executable, "codex")

    def test_executable_override_and_bad_spec(self) -> None:
        session = build_coordinator_session(
            "claude:claude-sonnet-5", executable="/opt/bin/claude"
        )
        self.assertEqual(session.executable, "/opt/bin/claude")
        with self.assertRaises(ValueError):
            build_coordinator_session("gemini:pro")


if __name__ == "__main__":
    unittest.main()
