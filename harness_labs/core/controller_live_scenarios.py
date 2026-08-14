"""Run the three flexibility scenarios with live Codex workers."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_labs.core.codex_agent_session import CodexAppServerSession
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import RunContract, RunLimits
from harness_labs.core.controller_live import CodexSemanticTaskExecutor
from harness_labs.core.controller_run import ControllerRunResult, run_controller
from harness_labs.core.controller_scheduler import RoleProfile


_COORDINATOR_INSTRUCTIONS = """\
You are the accountable run coordinator. You cannot read the repository or run
commands yourself. Use only the typed controller query and command tools.
Decompose the objective into bounded tasks assigned to available role profiles.
Inspect task results and open material artifacts before making downstream
decisions. Treat worker claims as untrusted until their evidence is present.

Every dispatched task must use one advertised role, capability set, and detail
schema. Its context must be a JSON object encoded as a string. Include an
"artifact_kind" key naming the deliverable kind. When downstream work depends on
an upstream report, open the upstream artifact and include the relevant content
or artifact reference in the downstream task context. Parallelize independent
work, but obey the run limits. Use unique stable task IDs.

Do not request completion until every operator criterion is satisfied, every
terminal artifact kind exists, required independent reviewers have reported,
and every disposition-required finding has a recorded disposition. If a gate
cannot be met, request a durable blocked outcome with the exact reason.
"""


@dataclass(frozen=True)
class LiveScenario:
    name: str
    contract: RunContract
    profiles: tuple[tuple[str, str, frozenset[str], str, frozenset[str]], ...]


def scenario_definitions(
    repository: Path,
    *,
    run_stamp: str,
) -> dict[str, LiveScenario]:
    head = _git_head(repository)
    return {
        "history-plan": LiveScenario(
            "history-plan",
            RunContract(
                run_id=f"{run_stamp}-history-plan",
                objective=(
                    f"Against Retinology at immutable head {head}: (1) inspect exactly "
                    "the most recent 10 commits and ground the survey; (2) identify and "
                    "record the next immediate implementation gap; (3) produce an "
                    "initial implementation plan; (4) dispatch exactly two independent "
                    "adversarial plan reviewers in parallel; (5) inspect and adjudicate "
                    "their findings; and (6) produce a revised, implementation-ready "
                    "final plan. Use artifact kinds commit-survey, initial-plan, "
                    "plan-review, and final-plan respectively. Assign the two reviewers "
                    "the independent-reviews criterion. Assign final-plan-ready only to "
                    "the revising planner. Include opened upstream report content in "
                    "dependent task contexts. Do not implement anything."
                ),
                phases=("active",),
                criteria=(
                    {
                        "id": "ten-commit-survey",
                        "statement": (
                            "Exactly the ten commits ending at the recorded head are "
                            "analyzed with commit-level evidence."
                        ),
                        "source": "operator",
                    },
                    {
                        "id": "gap-selected",
                        "statement": (
                            "The immediate implementation gap is justified from the "
                            "survey and repository state."
                        ),
                        "source": "operator",
                    },
                    {
                        "id": "independent-reviews",
                        "statement": (
                            "Two independent adversarial reviews assess correctness, "
                            "scope, sequencing, and verification."
                        ),
                        "source": "operator",
                        "minimum_satisfiers": 2,
                    },
                    {
                        "id": "final-plan-ready",
                        "statement": (
                            "The final plan adjudicates reviewer feedback and is "
                            "specific enough for implementation."
                        ),
                        "source": "operator",
                    },
                ),
                terminal_artifact_kinds=("final-plan",),
                limits=RunLimits(2, 8, 2, 10),
                repository={"path": str(repository), "head": head},
            ),
            profiles=(
                (
                    "codex-history",
                    "history_researcher",
                    frozenset({"repo.read", "git.read"}),
                    (
                        "Inspect exactly the requested commit range with git. Explain "
                        "each commit's purpose and cumulative direction, then identify "
                        "evidence-backed unfinished seams. Verify HEAD before analysis."
                    ),
                    frozenset({"commit-survey-details/1"}),
                ),
                (
                    "codex-planner",
                    "implementation_planner",
                    frozenset({"repo.read", "git.read"}),
                    (
                        "Convert grounded repository evidence into a bounded plan with "
                        "files/symbols, ordered changes, acceptance checks, risks, and "
                        "explicit exclusions. For revision work, adjudicate every "
                        "review finding explicitly."
                    ),
                    frozenset({"implementation-plan-details/1"}),
                ),
                (
                    "codex-reviewer",
                    "adversarial_reviewer",
                    frozenset({"repo.read", "git.read"}),
                    (
                        "Independently falsify the supplied plan against the repository. "
                        "Report only evidence-backed findings, including omissions, "
                        "incorrect assumptions, scope hazards, and weak verification. "
                        "Set requires_disposition true for material findings."
                    ),
                    frozenset({"plan-review-details/1"}),
                ),
            ),
        ),
        "dark-mode-ui": LiveScenario(
            "dark-mode-ui",
            RunContract(
                run_id=f"{run_stamp}-dark-mode-ui",
                objective=(
                    f"Against Retinology at immutable head {head}, determine visual "
                    "inconsistencies in dark mode on the Import and Process tabs. "
                    "Choose and dispatch between 2 and 4 independent inspectors in "
                    "parallel, covering source/CSS inspection, the UI graph, and at "
                    "least one real Playwright browser walk. The browser capability "
                    "adapter will execute a fixed controller-owned Playwright command "
                    "and place its complete receipt in the browser inspector's context; "
                    "the inspector must report that exact result without rerunning it. "
                    "Then dispatch a synthesizer to open all "
                    "reports, deduplicate findings while preserving source IDs, "
                    "diagnose root causes, and propose a bounded fix. Inspector artifact "
                    "kinds are ui-inspection; the terminal synthesis artifact kind is "
                    "ui-diagnosis. Assign browser-walk only to the browser inspector and "
                    "ui-diagnosis-ready only to the synthesizer. Do not modify the repo."
                ),
                phases=("active",),
                criteria=(
                    {
                        "id": "browser-walk",
                        "statement": (
                            "A real isolated Playwright walk executes successfully and "
                            "its observed result is recorded."
                        ),
                        "source": "operator",
                    },
                    {
                        "id": "ui-diagnosis-ready",
                        "statement": (
                            "Import and Process dark-mode inconsistencies are "
                            "deduplicated, evidence-backed, and mapped to proposed fixes."
                        ),
                        "source": "operator",
                    },
                ),
                terminal_artifact_kinds=("ui-diagnosis",),
                limits=RunLimits(2, 6, 4, 8),
                repository={"path": str(repository), "head": head},
            ),
            profiles=(
                (
                    "codex-ui-source",
                    "ui_source_inspector",
                    frozenset({"repo.read"}),
                    (
                        "Trace Import and Process markup, CSS variables, component-local "
                        "colors, dark-mode selectors, and tests. Compare equivalent "
                        "surfaces and cite exact paths/lines."
                    ),
                    frozenset({"ui-inspection-details/1"}),
                ),
                (
                    "codex-ui-graph",
                    "ui_graph_inspector",
                    frozenset({"repo.read", "ui_graph.read"}),
                    (
                        "Inspect the repository's UI graph/contracts and map Import and "
                        "Process nodes to their rendered components. Identify graph-to-UI "
                        "parity gaps and cite exact graph evidence."
                    ),
                    frozenset({"ui-inspection-details/1"}),
                ),
                (
                    "codex-ui-browser",
                    "ui_browser_inspector",
                    frozenset({"repo.read", "browser.inspect", "playwright.inspect"}),
                    (
                        "Inspect the controller_verified_command receipt supplied by the "
                        "capability adapter. Record its exact exit status and PASS/FAIL "
                        "observations; do not rerun the command inside the read-only model "
                        "sandbox. "
                        "Then inspect dark-mode DOM/computed-style behavior for Import "
                        "and Process using existing browser tooling where possible. Do "
                        "not claim visual evidence that was not observed."
                    ),
                    frozenset({"ui-inspection-details/1"}),
                ),
                (
                    "codex-ui-synthesis",
                    "ui_synthesizer",
                    frozenset({"repo.read"}),
                    (
                        "Synthesize only supplied inspector evidence. Deduplicate by "
                        "root cause, retain each source finding ID in the prose, separate "
                        "confirmed observations from inference, and propose the smallest "
                        "file-specific correction plus live verification."
                    ),
                    frozenset({"ui-diagnosis-details/1"}),
                ),
            ),
        ),
        "idealized-product": LiveScenario(
            "idealized-product",
            RunContract(
                run_id=f"{run_stamp}-idealized-product",
                objective=(
                    f"Against Retinology at immutable head {head}, critically appraise "
                    "the repository across architecture, functionality, and UI design. "
                    "Choose an appropriate bounded decomposition and parallelize "
                    "independent domain reviews. Synthesize a current-state appraisal, "
                    "then define an idealized Retinology, map current-to-ideal gaps, and "
                    "produce a proposal for a website promoting that ideal product. "
                    "Use terminal artifact kinds current-appraisal, idealized-version, "
                    "gap-analysis, and website-proposal. Assign each matching criterion "
                    "only to its final synthesizing task. Open and pass relevant upstream "
                    "reports into dependent contexts. Do not modify the repository."
                ),
                phases=("active",),
                criteria=(
                    {
                        "id": "current-appraisal-ready",
                        "statement": (
                            "Architecture, functionality, and UI are critically "
                            "appraised with repository evidence."
                        ),
                        "source": "operator",
                    },
                    {
                        "id": "ideal-product-ready",
                        "statement": (
                            "An internally coherent ideal product is defined for its "
                            "users, workflows, trust model, and technical shape."
                        ),
                        "source": "operator",
                    },
                    {
                        "id": "gap-analysis-ready",
                        "statement": (
                            "Traceable gaps connect the current appraisal to the ideal."
                        ),
                        "source": "operator",
                    },
                    {
                        "id": "website-proposal-ready",
                        "statement": (
                            "A concrete information architecture and narrative promote "
                            "the ideal product without unsupported claims."
                        ),
                        "source": "operator",
                    },
                ),
                terminal_artifact_kinds=(
                    "current-appraisal",
                    "idealized-version",
                    "gap-analysis",
                    "website-proposal",
                ),
                limits=RunLimits(3, 8, 4, 14),
                repository={"path": str(repository), "head": head},
            ),
            profiles=(
                (
                    "codex-architecture",
                    "architecture_critic",
                    frozenset({"repo.read", "git.read"}),
                    (
                        "Appraise boundaries, data authority, dependency direction, "
                        "safety properties, operability, tests, and architectural debt. "
                        "Distinguish designed intent from implemented reality."
                    ),
                    frozenset({"repository-appraisal-details/1"}),
                ),
                (
                    "codex-functionality",
                    "functionality_critic",
                    frozenset({"repo.read"}),
                    (
                        "Trace actual user workflows and functional completeness from "
                        "entry points through APIs, persistence, error states, and tests."
                    ),
                    frozenset({"repository-appraisal-details/1"}),
                ),
                (
                    "codex-ui",
                    "ui_design_critic",
                    frozenset({"repo.read", "ui_graph.read"}),
                    (
                        "Appraise information architecture, interaction consistency, "
                        "accessibility, responsive behavior, visual system, and the "
                        "relationship between UI graph intent and implementation."
                    ),
                    frozenset({"repository-appraisal-details/1"}),
                ),
                (
                    "codex-product",
                    "product_synthesizer",
                    frozenset({"repo.read"}),
                    (
                        "Integrate supplied domain evidence into a coherent appraisal or "
                        "ideal product definition. Preserve disagreements and uncertainty."
                    ),
                    frozenset(
                        {
                            "current-appraisal-details/1",
                            "ideal-product-details/1",
                        }
                    ),
                ),
                (
                    "codex-gap",
                    "gap_analyst",
                    frozenset({"repo.read"}),
                    (
                        "Build a traceable current-to-ideal gap matrix with impact, "
                        "dependencies, ordering, and measurable closure criteria."
                    ),
                    frozenset({"gap-analysis-details/1"}),
                ),
                (
                    "codex-website",
                    "website_strategist",
                    frozenset({"repo.read"}),
                    (
                        "Propose positioning, audiences, evidence-safe claims, page "
                        "architecture, page-level messages, calls to action, and visual "
                        "direction for the idealized product."
                    ),
                    frozenset({"website-proposal-details/1"}),
                ),
            ),
        ),
    }


def run_live_scenario(
    scenario: LiveScenario,
    *,
    repository: Path,
    run_dir: Path,
    model: str,
    reasoning: str,
) -> ControllerRunResult:
    """Execute one scenario using a resident Codex coordinator and live workers."""

    def session_builder(evidence: EvidenceCatalog) -> CodexAppServerSession:
        return CodexAppServerSession(
            model=model,
            reasoning=reasoning,
            timeout_seconds=900,
            persistent_rollout=True,
            base_instructions=_COORDINATOR_INSTRUCTIONS,
            audit=evidence.audit,
        )

    def profile_builder(evidence: EvidenceCatalog) -> tuple[RoleProfile, ...]:
        profiles = []
        for profile_id, role, capabilities, instructions, details_schemas in (
            scenario.profiles
        ):
            profiles.append(
                RoleProfile(
                    profile_id=profile_id,
                    role=role,
                    capabilities=capabilities,
                    backend_id="codex-exec",
                    details_schemas=details_schemas,
                    executor_factory=(
                        lambda task,
                        role_instructions=instructions,
                        profile_role=role: CodexSemanticTaskExecutor(
                            task=task,
                            repository=repository,
                            evidence=evidence,
                            role_instructions=role_instructions,
                            model=model,
                            reasoning=reasoning,
                            timeout_seconds=900,
                            preflight_argv=(
                                (
                                    "/Users/kirillzaslavsky/claudeprojects/"
                                    "Retinology/.venv/bin/python",
                                    "scripts/live_walk_minimalist_ui.py",
                                    "--require-browser",
                                    "--port",
                                    "8424",
                                )
                                if profile_role == "ui_browser_inspector"
                                else ()
                            ),
                            audit=evidence.audit,
                        )
                    ),
                )
            )
        return tuple(profiles)

    return run_controller(
        scenario.contract,
        session_builder=session_builder,
        profile_builder=profile_builder,
        run_dir=run_dir,
        max_tool_calls=96,
        evidence_classification="production_lifecycle",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(
            "/Users/kirillzaslavsky/.codex/worktrees/"
            "retinology-main-promotion-20260803"
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=("all", "history-plan", "dark-mode-ui", "idealized-product"),
        default="all",
    )
    parser.add_argument("--run-root", type=Path, default=Path("logs/runs"))
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--reasoning", default="medium")
    args = parser.parse_args(argv)

    repository = args.repository.resolve(strict=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    definitions = scenario_definitions(repository, run_stamp=stamp)
    names = tuple(definitions) if args.scenario == "all" else (args.scenario,)
    summaries: list[dict[str, Any]] = []
    for name in names:
        scenario = definitions[name]
        run_dir = args.run_root / scenario.contract.run_id
        result = run_live_scenario(
            scenario,
            repository=repository,
            run_dir=run_dir,
            model=args.model,
            reasoning=args.reasoning,
        )
        summary = {
            "scenario": name,
            "run_id": scenario.contract.run_id,
            "run_dir": str(result.run_dir),
            "coordinator_status": result.result.status,
            "run_status": result.run_view["status"],
            "completion_failures": result.run_view["completion_failures"],
            "artifacts": result.run_view["artifacts"],
            "tasks": result.run_view["tasks"],
        }
        summaries.append(summary)
        print(json.dumps(summary, sort_keys=True))
        if result.run_view["status"] != "succeeded":
            return 1
    print(json.dumps({"live_scenarios": summaries}, sort_keys=True))
    return 0


def _git_head(repository: Path) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("repository HEAD cannot be resolved")
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
