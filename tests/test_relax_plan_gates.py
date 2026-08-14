"""CB-02: validate_plan_graph_plan drops the verbatim-substring plan gates
while every referential integrity check it performs keeps rejecting.

Self-contained: imports only symbols that exist at the frozen base commit
(6fb1fd1c748d65c65d06b3a115887e9045f3e5d0) and exercises the real
validate_plan_graph_plan entry point directly, with no fixtures shared with
tests/test_plan_graph.py.
"""

from __future__ import annotations

import unittest

from harness_labs.plangraph.plan_graph import (
    PLAN_GRAPH_PROTOCOL,
    PathIntent,
    PlanGraphError,
    PlanGraphPlan,
    PlanRun,
    validate_plan_graph_plan,
)


def _plan(runs, plan_sections, acceptance_criteria, *, protocol=None) -> PlanGraphPlan:
    fields = dict(
        plan="docs/approved-plan.md",
        base_commit="deadbeef",
        runs=tuple(runs),
        plan_sections=plan_sections,
        acceptance_criteria=acceptance_criteria,
    )
    if protocol is not None:
        fields["protocol"] = protocol
    return PlanGraphPlan(**fields)


def _run(**overrides) -> PlanRun:
    fields = dict(
        id="a",
        objective="Build the widget loader",
        plan_sections=("1",),
        criteria=("AC-1",),
        depends_on=(),
        verification_argv=("python3", "-m", "unittest"),
        allowed_paths=("widget.py",),
    )
    fields.update(overrides)
    return PlanRun(**fields)


class VerbatimPlanGateRelaxationTests(unittest.TestCase):
    """AC-CB02-1: cited plan sections may describe a run without quoting it."""

    def test_accepts_run_when_objective_is_not_verbatim_in_cited_sections(self) -> None:
        plan = _plan(
            runs=[_run(objective="Build the widget loader")],
            plan_sections={
                "1": "Section 1 covers the loader subsystem. AC-1: Loader initializes without error.",
            },
            acceptance_criteria={"AC-1": "Loader initializes without error."},
        )

        validate_plan_graph_plan(plan)

    def test_accepts_run_when_criterion_statement_is_not_verbatim_in_cited_sections(self) -> None:
        plan = _plan(
            runs=[_run(objective="Build the widget loader")],
            plan_sections={
                "1": "Build the widget loader. Section 1 covers the loader subsystem and its AC-1 obligation.",
            },
            acceptance_criteria={"AC-1": "Loader initializes without error."},
        )

        validate_plan_graph_plan(plan)

    def test_accepts_engineered_plan_with_zero_verbatim_overlap(self) -> None:
        plan = _plan(
            runs=[
                _run(
                    id="a",
                    objective="Ship the ingest pipeline",
                    plan_sections=("intro",),
                    criteria=("AC-1",),
                    allowed_paths=("pipeline.py",),
                )
            ],
            plan_sections={
                "intro": "This part of the design describes how records flow from the "
                "queue into storage and what validation happens along the way.",
            },
            acceptance_criteria={"AC-1": "Malformed records are rejected before storage."},
        )

        validate_plan_graph_plan(plan)


class ReferentialIntegrityPreservedTests(unittest.TestCase):
    """AC-CB02-2: every non-substring integrity check still rejects."""

    def test_rejects_unknown_plan_section_key(self) -> None:
        plan = _plan(
            runs=[_run(plan_sections=("missing-section",))],
            plan_sections={"1": "Build the widget loader. AC-1: Loader initializes without error."},
            acceptance_criteria={"AC-1": "Loader initializes without error."},
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)

    def test_rejects_criterion_assigned_to_no_run(self) -> None:
        plan = _plan(
            runs=[_run(criteria=())],
            plan_sections={"1": "Build the widget loader."},
            acceptance_criteria={"AC-1": "Loader initializes without error."},
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)

    def test_rejects_run_referencing_unknown_criterion(self) -> None:
        plan = _plan(
            runs=[_run(criteria=("AC-404",))],
            plan_sections={"1": "Build the widget loader."},
            acceptance_criteria={},
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)

    def test_rejects_dependency_on_unknown_run(self) -> None:
        plan = _plan(
            runs=[_run(depends_on=("ghost",))],
            plan_sections={"1": "Build the widget loader. AC-1: Loader initializes without error."},
            acceptance_criteria={"AC-1": "Loader initializes without error."},
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)

    def test_rejects_cyclic_dependency(self) -> None:
        plan = _plan(
            runs=[
                _run(id="a", criteria=("AC-1",), depends_on=("b",), plan_sections=("1",)),
                _run(id="b", objective="Build the widget renderer", criteria=("AC-2",), depends_on=("a",), plan_sections=("2",)),
            ],
            plan_sections={
                "1": "Build the widget loader. AC-1: Loader initializes without error.",
                "2": "Build the widget renderer. AC-2: Renderer draws the widget.",
            },
            acceptance_criteria={
                "AC-1": "Loader initializes without error.",
                "AC-2": "Renderer draws the widget.",
            },
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)

    def test_rejects_empty_allowed_paths_under_canonical_protocol(self) -> None:
        plan = _plan(
            runs=[_run(allowed_paths=())],
            plan_sections={"1": "Build the widget loader. AC-1: Loader initializes without error."},
            acceptance_criteria={"AC-1": "Loader initializes without error."},
            protocol=PLAN_GRAPH_PROTOCOL,
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)

    def test_rejects_path_intent_outside_allowed_paths(self) -> None:
        plan = _plan(
            runs=[
                _run(
                    allowed_paths=("widget.py",),
                    path_intents=(PathIntent("other.py", "modify"),),
                )
            ],
            plan_sections={"1": "Build the widget loader. AC-1: Loader initializes without error."},
            acceptance_criteria={"AC-1": "Loader initializes without error."},
        )

        with self.assertRaises(PlanGraphError):
            validate_plan_graph_plan(plan)


if __name__ == "__main__":
    unittest.main()
