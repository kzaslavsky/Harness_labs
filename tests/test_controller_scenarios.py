"""Flexibility suite: three materially different coordinator task graphs."""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from harness_labs.core.attempts import TaskResult
from harness_labs.core.controller_coordinator import CoordinatorLoop
from harness_labs.core.controller_evidence import EvidenceCatalog, EvidenceRecord
from harness_labs.core.controller_kernel import ControllerKernel, RunContract, RunLimits
from harness_labs.core.controller_projection import ControllerQueries
from harness_labs.core.controller_results import semantic_payload
from harness_labs.core.controller_scheduler import CapabilityScheduler, RoleProfile

from tests.controller_scenario_fixtures import (
    FixtureExecutor,
    ScriptedCoordinatorSession,
    task,
)


def descriptor(record: EvidenceRecord) -> dict:
    return record.as_dict()


def coverage(criterion_id: str, ref: str) -> dict:
    return {
        "criterion_id": criterion_id,
        "status": "satisfied",
        "evidence_refs": [ref],
    }


class ControllerScenarioTests(unittest.TestCase):
    def test_history_gap_plan_review_revise(self) -> None:
        evidence = EvidenceCatalog()
        artifacts = {
            "survey": evidence.add(
                kind="commit-survey",
                content={"head": "abc123", "commits": [f"sha-{i}" for i in range(10)]},
                media_type="application/json",
                producer_task_id="survey-history",
            ),
            "draft": evidence.add(
                kind="initial-plan",
                content="# Initial plan",
                media_type="text/markdown",
                producer_task_id="draft-plan",
            ),
            "review-a": evidence.add(
                kind="plan-review",
                content="Review A",
                media_type="text/plain",
                producer_task_id="review-a",
            ),
            "review-b": evidence.add(
                kind="plan-review",
                content="Review B",
                media_type="text/plain",
                producer_task_id="review-b",
            ),
            "final": evidence.add(
                kind="final-plan",
                content="# Revised plan",
                media_type="text/markdown",
                producer_task_id="revise-plan",
            ),
        }
        contract = RunContract(
            run_id="history-plan",
            objective="Inspect ten commits, identify a gap, plan, review, and revise.",
            phases=("active",),
            criteria=(
                {
                    "id": "history",
                    "statement": "Exactly ten commits are grounded.",
                    "source": "operator",
                },
                {
                    "id": "plan",
                    "statement": "The selected gap has an implementation plan.",
                    "source": "operator",
                },
                {
                    "id": "review",
                    "statement": "Two independent reviews are complete.",
                    "source": "operator",
                    "minimum_satisfiers": 2,
                },
                {
                    "id": "revision",
                    "statement": "Accepted findings are reflected.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("final-plan",),
            limits=RunLimits(2, 8, 2, 8),
            repository={"head": "abc123"},
        )
        kernel = ControllerKernel(contract, evidence=evidence)

        def build(task_value, attempt):
            task_id = task_value["id"]
            if task_id == "survey-history":
                artifact = artifacts["survey"]
                payload = semantic_payload(
                    summary="Surveyed exactly ten commits at abc123.",
                    details_schema=task_value["details_schema"],
                    details={
                        "head": "abc123",
                        "commits": [f"sha-{i}" for i in range(10)],
                        "gap_candidates": ["missing integration boundary"],
                    },
                    artifacts=(descriptor(artifact),),
                    claims=(
                        {
                            "id": "ten-commits",
                            "statement": "Exactly ten commits were inspected.",
                            "kind": "observed",
                            "evidence_refs": [artifact.ref],
                        },
                    ),
                    criterion_coverage=(coverage("history", artifact.ref),),
                )
            elif task_id == "draft-plan":
                artifact = artifacts["draft"]
                payload = semantic_payload(
                    summary="Planned the selected integration gap.",
                    details_schema=task_value["details_schema"],
                    details={"decision_ref": "decision:selected-gap"},
                    artifacts=(descriptor(artifact),),
                    criterion_coverage=(coverage("plan", artifact.ref),),
                )
            elif task_id in {"review-a", "review-b"}:
                artifact = artifacts[task_id]
                local = task_id[-1]
                payload = semantic_payload(
                    summary=f"Reviewer {local} found one material issue.",
                    details_schema=task_value["details_schema"],
                    details={"plan_sha256": artifacts["draft"].sha256},
                    artifacts=(descriptor(artifact),),
                    findings=(
                        {
                            "id": "finding",
                            "statement": f"Review {local} correction is required.",
                            "category": "plan-quality",
                            "severity": "major",
                            "requires_disposition": True,
                            "evidence_refs": [artifact.ref],
                        },
                    ),
                    criterion_coverage=(coverage("review", artifact.ref),),
                )
            else:
                artifact = artifacts["final"]
                payload = semantic_payload(
                    summary="Revised the plan for both accepted findings.",
                    details_schema=task_value["details_schema"],
                    details={
                        "prior_plan_sha256": artifacts["draft"].sha256,
                        "incorporated": ["review-a/finding", "review-b/finding"],
                    },
                    artifacts=(descriptor(artifact),),
                    criterion_coverage=(coverage("revision", artifact.ref),),
                )
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="succeeded",
                payload=payload,
            )

        profiles = (
            RoleProfile(
                "history-reader",
                "history_researcher",
                frozenset({"repo.read", "git.read"}),
                lambda item: FixtureExecutor(item, build),
            ),
            RoleProfile(
                "planner",
                "planner",
                frozenset({"repo.read"}),
                lambda item: FixtureExecutor(item, build),
            ),
            RoleProfile(
                "reviewer",
                "adversarial_reviewer",
                frozenset({"repo.read"}),
                lambda item: FixtureExecutor(item, build),
            ),
        )
        session = ScriptedCoordinatorSession(
            [
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "survey-history",
                                "history_researcher",
                                "Inspect exactly ten commits at abc123",
                                "commit-survey-details/1",
                                capabilities=("repo.read", "git.read"),
                                criteria=("history",),
                            )
                        ],
                        "max_parallelism": 1,
                    },
                ),
                (
                    "decision_record",
                    {
                        "id": "selected-gap",
                        "question": "What is the immediate implementation gap?",
                        "choice": "Missing integration boundary",
                        "alternatives": ["UI polish", "broad refactor"],
                        "rationale": "The commit survey shows an unfinished boundary.",
                        "evidence_refs": [artifacts["survey"].ref],
                    },
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "draft-plan",
                                "planner",
                                "Plan the selected gap",
                                "implementation-plan-details/1",
                                capabilities=("repo.read",),
                                criteria=("plan",),
                                dependencies=("survey-history",),
                            )
                        ],
                        "max_parallelism": 1,
                    },
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "review-a",
                                "adversarial_reviewer",
                                "Review correctness",
                                "implementation-plan-details/1",
                                capabilities=("repo.read",),
                                criteria=("review",),
                                dependencies=("draft-plan",),
                            ),
                            task(
                                "review-b",
                                "adversarial_reviewer",
                                "Review scope and verification",
                                "implementation-plan-details/1",
                                capabilities=("repo.read",),
                                criteria=("review",),
                                dependencies=("draft-plan",),
                            ),
                        ],
                        "max_parallelism": 2,
                    },
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "revise-plan",
                                "planner",
                                "Revise the plan from accepted findings",
                                "implementation-plan-details/1",
                                capabilities=("repo.read",),
                                criteria=("revision",),
                                dependencies=("review-a", "review-b"),
                            )
                        ],
                        "max_parallelism": 1,
                    },
                ),
                (
                    "finding_disposition",
                    {
                        "finding_id": "review-a/finding",
                        "disposition": "resolved",
                        "rationale": "Incorporated in the revised plan.",
                        "resolution_refs": [artifacts["final"].ref],
                    },
                ),
                (
                    "finding_disposition",
                    {
                        "finding_id": "review-b/finding",
                        "disposition": "resolved",
                        "rationale": "Incorporated in the revised plan.",
                        "resolution_refs": [artifacts["final"].ref],
                    },
                ),
                ("run_complete_request", {}),
            ],
            final="The revised implementation plan is complete.",
        )
        scheduler = CapabilityScheduler(profiles)

        result = CoordinatorLoop(
            kernel,
            ControllerQueries(kernel, evidence),
            scheduler,
            session,
        ).run()

        self.assertEqual(result.status, "succeeded", result.payload)
        state = kernel.snapshot()
        self.assertEqual(len(state["tasks"]), 5)
        self.assertEqual(
            state["tasks"]["survey-history"]["result"]["details"]["head"],
            "abc123",
        )
        self.assertEqual(
            len(state["tasks"]["survey-history"]["result"]["details"]["commits"]),
            10,
        )
        self.assertEqual(state["criteria"]["review"]["satisfied_by"], [
            "review-a",
            "review-b",
        ])
        self.assertEqual(
            state["findings"]["review-a/finding"]["disposition"]["disposition"],
            "resolved",
        )

    def test_dynamic_parallel_dark_mode_diagnosis(self) -> None:
        evidence = EvidenceCatalog()
        screenshots = {
            task_id: evidence.add(
                kind="ui-screenshot",
                content=f"dark screenshot {task_id}",
                media_type="image/png",
                producer_task_id=task_id,
            )
            for task_id in ("inspect-import", "inspect-process", "inspect-graph")
        }
        diagnosis = evidence.add(
            kind="dark-mode-diagnosis",
            content="# Diagnosis\nNormalize token usage.",
            media_type="text/markdown",
            producer_task_id="synthesize-ui",
        )
        contract = RunContract(
            run_id="dark-mode",
            objective="Diagnose dark-mode inconsistencies on Import and Process.",
            phases=("active",),
            criteria=(
                {
                    "id": "diagnosed",
                    "statement": "Evidence-backed inconsistencies are synthesized.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("dark-mode-diagnosis",),
            limits=RunLimits(2, 6, 4, 10),
        )
        kernel = ControllerKernel(contract, evidence=evidence)

        def inspector_build(task_value, attempt):
            time.sleep(0.03)
            artifact = screenshots[task_value["id"]]
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="succeeded",
                payload=semantic_payload(
                    summary=f"Inspected {task_value['id']}.",
                    details_schema=task_value["details_schema"],
                    details={
                        "route": task_value["id"],
                        "theme": "dark",
                        "viewport": "1440x900",
                    },
                    artifacts=(descriptor(artifact),),
                    findings=(
                        {
                            "id": "token-mismatch",
                            "statement": "A surface uses the wrong dark token.",
                            "category": "dark-mode",
                            "severity": "major",
                            "requires_disposition": False,
                            "evidence_refs": [artifact.ref],
                        },
                    ),
                ),
            )

        def synthesis_build(task_value, attempt):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="succeeded",
                payload=semantic_payload(
                    summary="Deduplicated three observations into one diagnosis.",
                    details_schema=task_value["details_schema"],
                    details={
                        "symptom": "inconsistent dark surface token",
                        "cause": "component-local color overrides",
                        "proposed_fix": "use shared semantic surface tokens",
                    },
                    artifacts=(descriptor(diagnosis),),
                    findings=(
                        {
                            "id": "deduplicated-token-mismatch",
                            "statement": "Import and Process use inconsistent dark surfaces.",
                            "category": "dark-mode",
                            "severity": "major",
                            "requires_disposition": False,
                            "evidence_refs": [
                                item.ref for item in screenshots.values()
                            ],
                            "source_finding_ids": [
                                "inspect-import/token-mismatch",
                                "inspect-process/token-mismatch",
                                "inspect-graph/token-mismatch",
                            ],
                        },
                    ),
                    criterion_coverage=(coverage("diagnosed", diagnosis.ref),),
                ),
            )

        profiles = (
            RoleProfile(
                "visual-inspector",
                "ui_inspector",
                frozenset(
                    {
                        "repo.read",
                        "browser.inspect",
                        "playwright.inspect",
                        "ui_graph.read",
                    }
                ),
                lambda item: FixtureExecutor(item, inspector_build),
            ),
            RoleProfile(
                "ui-synthesizer",
                "ui_synthesizer",
                frozenset({"repo.read"}),
                lambda item: FixtureExecutor(item, synthesis_build),
            ),
        )
        inspectors = [
            task(
                task_id,
                "ui_inspector",
                f"Inspect {task_id}",
                "visual-inspection-details/1",
                capabilities=(
                    "repo.read",
                    "browser.inspect",
                    "playwright.inspect",
                    "ui_graph.read",
                ),
            )
            for task_id in screenshots
        ]
        session = ScriptedCoordinatorSession(
            [
                (
                    "task_dispatch",
                    {"tasks": inspectors, "max_parallelism": len(inspectors)},
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "synthesize-ui",
                                "ui_synthesizer",
                                "Deduplicate and diagnose findings",
                                "visual-inspection-details/1",
                                capabilities=("repo.read",),
                                criteria=("diagnosed",),
                                dependencies=tuple(screenshots),
                            )
                        ],
                        "max_parallelism": 1,
                    },
                ),
                ("run_complete_request", {}),
            ],
            final="Dark-mode diagnosis complete.",
        )
        scheduler = CapabilityScheduler(profiles)

        result = CoordinatorLoop(
            kernel,
            ControllerQueries(kernel, evidence),
            scheduler,
            session,
        ).run()

        self.assertEqual(result.status, "succeeded")
        self.assertGreaterEqual(scheduler.maximum_active, 2)
        state = kernel.snapshot()
        inspectors_state = [
            item for item in state["tasks"].values() if item["role"] == "ui_inspector"
        ]
        self.assertEqual(len(inspectors_state), 3)
        finding = state["findings"][
            "synthesize-ui/deduplicated-token-mismatch"
        ]
        self.assertEqual(len(finding["source_finding_ids"]), 3)
        self.assertEqual(len(finding["evidence_refs"]), 3)

    def test_hierarchical_appraisal_ideal_gaps_and_website(self) -> None:
        evidence = EvidenceCatalog()
        artifact_specs = {
            "architecture-subchild": ("architecture-evidence", "Architecture"),
            "architecture-lead": ("architecture-appraisal", "Architecture appraisal"),
            "functionality": ("functionality-appraisal", "Functionality appraisal"),
            "ui-design": ("ui-appraisal", "UI appraisal"),
            "appraisal": ("current-appraisal", "Current product appraisal"),
            "ideal": ("idealized-version", "Idealized Retinology"),
            "gaps": ("gap-analysis", "Current-to-ideal gaps"),
            "website": ("website-proposal", "Website proposal"),
        }
        artifacts = {
            task_id: evidence.add(
                kind=kind,
                content=content,
                media_type="text/markdown",
                producer_task_id=task_id,
            )
            for task_id, (kind, content) in artifact_specs.items()
        }
        contract = RunContract(
            run_id="ideal-retinology",
            objective="Appraise Retinology and propose its ideal product and website.",
            phases=("active",),
            criteria=(
                {
                    "id": "appraised",
                    "statement": "Current state is critically appraised.",
                    "source": "operator",
                },
                {
                    "id": "idealized",
                    "statement": "An ideal product is defined.",
                    "source": "operator",
                },
                {
                    "id": "gaps",
                    "statement": "Current-to-ideal gaps are traceable.",
                    "source": "operator",
                },
                {
                    "id": "website",
                    "statement": "A website proposal promotes the ideal product.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=(
                "current-appraisal",
                "idealized-version",
                "gap-analysis",
                "website-proposal",
            ),
            limits=RunLimits(3, 8, 4, 16),
        )
        kernel = ControllerKernel(contract, evidence=evidence)

        def build(task_value, attempt):
            task_id = task_value["id"]
            artifact = artifacts[task_id]
            criterion_by_task = {
                "appraisal": "appraised",
                "ideal": "idealized",
                "gaps": "gaps",
                "website": "website",
            }
            details = {"task": task_id}
            claims = ()
            delegations = ()
            if task_id == "architecture-lead":
                delegations = (
                    {
                        "tasks": [
                            task(
                                "architecture-subchild",
                                "architecture_specialist",
                                "Inspect module boundaries",
                                "repository-appraisal-details/1",
                                capabilities=("repo.read",),
                                parent_task_id="architecture-lead",
                            )
                        ],
                        "max_parallelism": 1,
                    },
                )
            if task_id == "appraisal":
                details["upstream_tasks"] = [
                    "architecture-lead",
                    "functionality",
                    "ui-design",
                ]
                claims = (
                    {
                        "id": "current-state",
                        "statement": "The current appraisal integrates three domains.",
                        "kind": "inferred",
                        "evidence_refs": [
                            artifacts["architecture-lead"].ref,
                            artifacts["functionality"].ref,
                            artifacts["ui-design"].ref,
                        ],
                    },
                )
            elif task_id == "ideal":
                details["current_appraisal_ref"] = artifacts["appraisal"].ref
                details["users"] = ["clinicians", "researchers"]
            elif task_id == "gaps":
                details["current_ref"] = artifacts["appraisal"].ref
                details["ideal_ref"] = artifacts["ideal"].ref
                details["gap_matrix"] = [
                    {
                        "current": "fragmented workflow",
                        "ideal": "guided unified workflow",
                    }
                ]
            elif task_id == "website":
                details["ideal_ref"] = artifacts["ideal"].ref
                details["audiences"] = ["clinicians", "research leaders"]
                details["pages"] = ["home", "product", "evidence", "contact"]
                details["calls_to_action"] = ["Request a demonstration"]
                details["visual_direction"] = "clinical clarity with retinal imagery"
            criterion = criterion_by_task.get(task_id)
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="succeeded",
                payload=semantic_payload(
                    summary=f"Completed {task_id}.",
                    details_schema=task_value["details_schema"],
                    details=details,
                    claims=claims,
                    artifacts=(descriptor(artifact),),
                    criterion_coverage=(
                        (coverage(criterion, artifact.ref),) if criterion else ()
                    ),
                    delegation_requests=delegations,
                ),
            )

        roles = {
            "architecture_lead": {"repo.read"},
            "architecture_specialist": {"repo.read"},
            "functionality_critic": {"repo.read"},
            "ui_critic": {"repo.read", "browser.inspect"},
            "product_synthesizer": {"repo.read"},
            "gap_analyst": {"repo.read"},
            "website_strategist": {"repo.read"},
        }
        profiles = tuple(
            RoleProfile(
                f"profile-{role}",
                role,
                frozenset(capabilities),
                lambda item, builder=build: FixtureExecutor(item, builder),
            )
            for role, capabilities in roles.items()
        )
        session = ScriptedCoordinatorSession(
            [
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "architecture-lead",
                                "architecture_lead",
                                "Appraise architecture",
                                "repository-appraisal-details/1",
                                capabilities=("repo.read",),
                                may_delegate=True,
                            ),
                            task(
                                "functionality",
                                "functionality_critic",
                                "Appraise functionality",
                                "repository-appraisal-details/1",
                                capabilities=("repo.read",),
                            ),
                            task(
                                "ui-design",
                                "ui_critic",
                                "Appraise UI design",
                                "repository-appraisal-details/1",
                                capabilities=("repo.read", "browser.inspect"),
                            ),
                        ],
                        "max_parallelism": 3,
                    },
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "appraisal",
                                "product_synthesizer",
                                "Synthesize the critical appraisal",
                                "repository-appraisal-details/1",
                                capabilities=("repo.read",),
                                criteria=("appraised",),
                                dependencies=(
                                    "architecture-lead",
                                    "functionality",
                                    "ui-design",
                                ),
                            )
                        ]
                    },
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "ideal",
                                "product_synthesizer",
                                "Define the ideal product",
                                "ideal-product-details/1",
                                capabilities=("repo.read",),
                                criteria=("idealized",),
                                dependencies=("appraisal",),
                            )
                        ]
                    },
                ),
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "gaps",
                                "gap_analyst",
                                "Map current state to ideal state",
                                "gap-analysis-details/1",
                                capabilities=("repo.read",),
                                criteria=("gaps",),
                                dependencies=("ideal",),
                            ),
                            task(
                                "website",
                                "website_strategist",
                                "Propose the promotional website",
                                "website-proposal-details/1",
                                capabilities=("repo.read",),
                                criteria=("website",),
                                dependencies=("ideal",),
                            ),
                        ],
                        "max_parallelism": 2,
                    },
                ),
                ("run_complete_request", {}),
            ],
            final="Appraisal portfolio complete.",
        )

        result = CoordinatorLoop(
            kernel,
            ControllerQueries(kernel, evidence),
            CapabilityScheduler(profiles),
            session,
        ).run()

        self.assertEqual(result.status, "succeeded")
        state = kernel.snapshot()
        self.assertEqual(len(state["tasks"]), 8)
        self.assertEqual(state["tasks"]["architecture-subchild"]["depth"], 2)
        self.assertEqual(
            state["tasks"]["architecture-subchild"]["parent_task_id"],
            "architecture-lead",
        )
        self.assertEqual(
            state["tasks"]["gaps"]["result"]["details"]["current_ref"],
            artifacts["appraisal"].ref,
        )
        self.assertEqual(
            state["tasks"]["website"]["result"]["details"]["ideal_ref"],
            artifacts["ideal"].ref,
        )

    def test_kernel_and_command_handlers_contain_no_scenario_switches(self) -> None:
        package = Path(__file__).parents[1] / "harness_labs" / "core"
        source = "\n".join(
            (package / name).read_text(encoding="utf-8").lower()
            for name in (
                "controller_kernel.py",
                "controller_commands.py",
                "controller_projection.py",
                "controller_coordinator.py",
            )
        )
        for forbidden in (
            "retinology",
            "dark mode",
            "dark-mode",
            "commit-survey-details",
            "website-proposal-details",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
