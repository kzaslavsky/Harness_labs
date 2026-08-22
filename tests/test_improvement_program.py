"""Tests for the graphrun-layer improvement program (SI-04).

Covers AC-SI04-1 (finding_history recurrence join by path containment,
carrying campaign label/disposition/statement), AC-SI04-2 (decision-registry
governance annotation, uncited-governed-path refusal, unresolved
Inconsistency surfacing), AC-SI04-3 (bounded refusal on an unfillable
Complexity-admission triple or non-executable success criteria, and
gate_relaxation ruling-statement rejection), and AC-SI04-4 (injectable
judgment callable -- no live model -- and human-authored ruling validation
gating acceptance).
"""

from __future__ import annotations

import importlib.util
import unittest
from dataclasses import replace as replace_dataclass_fields
from pathlib import Path
from typing import Mapping

from harness_labs.core.decision_registry import load_decisions
from harness_labs.graphrun.improvement_program import (
    ProposalDraft,
    ProposalRefused,
    RulingError,
    SuccessCriterionDraft,
    TargetSurfaceDraft,
    annotate_pattern_with_recurrence,
    apply_ruling,
    draft_proposal,
    join_governing_decisions,
    join_recurrence,
    validate_gate_relaxation_ruling,
)
from harness_labs.plangraph.finding_history import fold_campaigns

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "improvement" / "memory"
CAMPAIGN_LAUNCHER_PATH = "harness_labs/graphrun/campaign_launcher.py"
GOVERNING_DECISION_ID = "0001-campaign-launcher-timeout-floor"

_checker_spec = importlib.util.spec_from_file_location(
    "check_improvement_artifacts", REPO_ROOT / "scripts" / "dev" / "check_improvement_artifacts.py"
)
checker = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(checker)


def _finding_history():
    return fold_campaigns(
        [
            (
                FIXTURES / "campaigns" / "prior-hardening" / "journal.jsonl",
                "prior-hardening",
                "harness-labs",
            ),
            (
                FIXTURES / "campaigns" / "cold-start-audit" / "journal.jsonl",
                "cold-start-audit",
                "harness-labs",
            ),
        ]
    )


def _decision_registry():
    return load_decisions(FIXTURES / "decisions")


def _pattern(**overrides) -> dict:
    pattern = {
        "protocol": "blocker-pattern/1",
        "pattern_id": "pattern-campaign-launcher-timeout",
        "signature": "timeout-swallowed-in-campaign-launcher",
        "classification": "harness_or_configuration",
        "status": "candidate",
        "cost_aggregate": {
            "wall_clock_ms": {"median": 4200, "tail": 9100},
            "tokens": {"median": 1200, "tail": 3000},
            "diff_churn_lines": {"median": 12, "tail": 40},
        },
        "generalizability": {
            "verdict": "policy_gap",
            "rubric_id": "burden-admission/1",
            "rationale": "recurs across attempts, not a one-off",
            "counterexamples": [],
        },
    }
    pattern.update(overrides)
    return pattern


def _executable_criterion(**overrides) -> SuccessCriterionDraft:
    criterion = SuccessCriterionDraft(
        file=CAMPAIGN_LAUNCHER_PATH,
        subject="silent-timeout-swallow",
        required_paths=(CAMPAIGN_LAUNCHER_PATH,),
        statement="the launcher must surface a swallowed retry timeout",
        assertion={
            "argv": ["python3", "-m", "pytest", "tests/test_campaign_launcher.py", "-q"],
            "timeout_seconds": 600,
        },
    )
    return replace_dataclass(criterion, **overrides)


def replace_dataclass(instance, **overrides):
    return replace_dataclass_fields(instance, **overrides) if overrides else instance


def _draft(**overrides) -> ProposalDraft:
    draft = ProposalDraft(
        question="how should the launcher surface a swallowed retry timeout?",
        choice="log and re-raise instead of swallowing",
        alternatives=("leave as-is",),
        rationale=(
            "recurring pattern across campaigns; governed by "
            f"{GOVERNING_DECISION_ID}"
        ),
        evidence=(f"see {GOVERNING_DECISION_ID} for the timeout floor",),
        consequences=("slightly noisier logs",),
        reversible=True,
        demonstrated_failure="observed in prior-hardening and cold-start-audit campaigns",
        production_consumer="scripts/self_improve.py round dispatch",
        end_to_end_assertion="pytest tests/test_campaign_launcher.py -q passes post-fix",
        target_surface=(TargetSurfaceDraft(path=CAMPAIGN_LAUNCHER_PATH, kind="code"),),
        accuracy_risk="none",
        success_criteria=(_executable_criterion(),),
        rollback="revert the commit",
    )
    return replace_dataclass(draft, **overrides)


class RecurrenceJoinTests(unittest.TestCase):
    """AC-SI04-1."""

    def test_recurrence_entries_carry_campaign_label_disposition_and_statement(
        self,
    ) -> None:
        history = _finding_history()
        entries = join_recurrence([CAMPAIGN_LAUNCHER_PATH], history=history)

        self.assertEqual(len(entries), 2)
        by_label = {entry.campaign_label: entry for entry in entries}
        self.assertIn("prior-hardening", by_label)
        self.assertIn("cold-start-audit", by_label)

        waived = by_label["prior-hardening"]
        self.assertEqual(waived.source, "finding_history")
        self.assertEqual(waived.disposition, "waive")
        self.assertIn("observability upgrade", waived.statement)
        self.assertEqual(
            waived.key, f"{CAMPAIGN_LAUNCHER_PATH}::silent-timeout-swallow"
        )

        repaired = by_label["cold-start-audit"]
        self.assertEqual(repaired.disposition, "require_repair")
        self.assertIn("Repair required", repaired.statement)

    def test_recurrence_join_respects_directory_containment(self) -> None:
        history = _finding_history()
        entries = join_recurrence(["harness_labs/graphrun"], history=history)
        self.assertEqual(len(entries), 2)

        no_match = join_recurrence(["harness_labs/observability"], history=history)
        self.assertEqual(no_match, ())

    def test_schema_dict_projection_matches_blocker_pattern_recurrence_shape(
        self,
    ) -> None:
        history = _finding_history()
        entries = join_recurrence([CAMPAIGN_LAUNCHER_PATH], history=history)
        for entry in entries:
            schema_dict = entry.to_schema_dict()
            self.assertEqual(set(schema_dict), {"source", "key", "ref"})
            self.assertEqual(schema_dict["source"], "finding_history")

    def test_annotate_pattern_with_recurrence_populates_blocker_pattern_field(
        self,
    ) -> None:
        """A pattern is annotated with its prior-ruled recurrence directly,
        independent of drafting a proposal (AC-SI04-1)."""

        pattern = _pattern()
        annotated = annotate_pattern_with_recurrence(
            pattern, [CAMPAIGN_LAUNCHER_PATH], history=_finding_history()
        )
        self.assertIsNot(annotated, pattern)
        self.assertNotIn("recurrence", pattern)
        self.assertEqual(len(annotated["recurrence"]), 2)
        for entry in annotated["recurrence"]:
            self.assertEqual(set(entry), {"source", "key", "ref"})
            self.assertEqual(entry["source"], "finding_history")

    def test_annotate_pattern_with_recurrence_is_empty_when_no_lineage_matches(
        self,
    ) -> None:
        pattern = _pattern()
        annotated = annotate_pattern_with_recurrence(
            pattern, ["harness_labs/observability"], history=_finding_history()
        )
        self.assertEqual(annotated["recurrence"], [])

    def test_drafted_proposal_ruling_packet_recurrence_carries_campaign_lineage(
        self,
    ) -> None:
        """Proves the join_recurrence-to-RulingPacket wiring inside
        draft_proposal, not just join_recurrence in isolation: swapping in an
        empty tuple at the call site would fail this assertion."""

        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-recurrence-wiring",
        )
        recurrence = proposal.ruling_packet.recurrence
        self.assertEqual(len(recurrence), 2)
        by_label = {entry.campaign_label: entry for entry in recurrence}
        self.assertEqual(by_label["prior-hardening"].disposition, "waive")
        self.assertEqual(by_label["cold-start-audit"].disposition, "require_repair")
        self.assertIn("Repair required", by_label["cold-start-audit"].statement)


class GovernanceJoinTests(unittest.TestCase):
    """AC-SI04-2."""

    def test_target_surface_path_annotated_with_governing_decision(self) -> None:
        registry = _decision_registry()
        per_path, inconsistencies = join_governing_decisions(
            [CAMPAIGN_LAUNCHER_PATH], registry=registry
        )
        self.assertEqual(per_path[CAMPAIGN_LAUNCHER_PATH], (GOVERNING_DECISION_ID,))
        # unrelated to this path, but still surfaced -- never scoped away.
        self.assertEqual(len(inconsistencies), 1)
        self.assertEqual(inconsistencies[0].superseded_id, "0002-legacy-retry-shim")
        self.assertEqual(
            inconsistencies[0].superseding_id, "0003-supersedes-legacy-retry-shim"
        )

    def test_ungoverned_path_has_no_governing_decisions(self) -> None:
        registry = _decision_registry()
        per_path, _ = join_governing_decisions(
            ["harness_labs/graphrun/agent_mixture.py"], registry=registry
        )
        self.assertEqual(per_path["harness_labs/graphrun/agent_mixture.py"], ())

    def test_drafter_refuses_a_governed_path_proposal_that_does_not_cite_the_adr(
        self,
    ) -> None:
        uncited_draft = _draft(rationale="a general improvement", evidence=())

        with self.assertRaises(ProposalRefused) as ctx:
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: uncited_draft,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-1",
            )
        self.assertIn("without citing", str(ctx.exception))
        self.assertIn(GOVERNING_DECISION_ID, str(ctx.exception))

    def test_drafter_emits_when_governed_path_is_cited(self) -> None:
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-2",
        )
        self.assertEqual(
            proposal.target_surface[0]["governing_decisions"],
            [GOVERNING_DECISION_ID],
        )

    def test_ruling_packet_surfaces_unresolved_registry_inconsistency(self) -> None:
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-3",
        )
        inconsistencies = proposal.ruling_packet.registry_inconsistencies
        self.assertEqual(len(inconsistencies), 1)
        self.assertEqual(
            inconsistencies[0],
            {
                "superseded_id": "0002-legacy-retry-shim",
                "superseding_id": "0003-supersedes-legacy-retry-shim",
            },
        )


class BoundedDraftingTests(unittest.TestCase):
    """AC-SI04-3 and AC-SI04-4 (injectable judgment)."""

    def test_judgment_is_an_injectable_callable_with_no_live_model(self) -> None:
        calls: list[Mapping] = []

        def stub_judgment(pattern):
            calls.append(pattern)
            return _draft()

        proposal = draft_proposal(
            _pattern(),
            judgment=stub_judgment,
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-4",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(proposal.status, "proposed")

    def test_judgment_declining_outright_refuses(self) -> None:
        with self.assertRaises(ProposalRefused) as ctx:
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: None,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-5",
            )
        self.assertIn("declined", str(ctx.exception))

    def test_refuses_when_demonstrated_failure_is_missing(self) -> None:
        incomplete = _draft(demonstrated_failure=None)
        with self.assertRaises(ProposalRefused) as ctx:
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: incomplete,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-6",
            )
        self.assertIn("Complexity-admission", str(ctx.exception))

    def test_refuses_when_production_consumer_is_blank(self) -> None:
        incomplete = _draft(production_consumer="   ")
        with self.assertRaises(ProposalRefused):
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: incomplete,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-7",
            )

    def test_refuses_when_no_success_criteria_are_executable(self) -> None:
        signature_only = SuccessCriterionDraft(
            file=CAMPAIGN_LAUNCHER_PATH,
            subject="silent-timeout-swallow",
            required_paths=(CAMPAIGN_LAUNCHER_PATH,),
            statement="the swallow signature must no longer appear",
            assertion={"signature_absent": "silent-timeout-swallow"},
        )
        draft_without_executable_assertion = _draft(
            success_criteria=(signature_only,)
        )
        with self.assertRaises(ProposalRefused) as ctx:
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: draft_without_executable_assertion,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-8",
            )
        self.assertIn("executable", str(ctx.exception))

    def test_refuses_when_executable_assertion_keys_are_present_but_vacuous(
        self,
    ) -> None:
        """An assertion carrying both required keys with values the schema
        itself rejects (empty argv, non-positive timeout_seconds) cannot
        execute and must not satisfy the refusal gate."""

        vacuous = SuccessCriterionDraft(
            file=CAMPAIGN_LAUNCHER_PATH,
            subject="silent-timeout-swallow",
            required_paths=(CAMPAIGN_LAUNCHER_PATH,),
            statement="the launcher must surface a swallowed retry timeout",
            assertion={"argv": [], "timeout_seconds": 0},
        )
        draft_with_vacuous_assertion = _draft(success_criteria=(vacuous,))
        with self.assertRaises(ProposalRefused) as ctx:
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: draft_with_vacuous_assertion,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-vacuous-assertion",
            )
        self.assertIn("executable", str(ctx.exception))

    def test_refuses_when_success_criteria_is_empty(self) -> None:
        draft_without_criteria = _draft(success_criteria=())
        with self.assertRaises(ProposalRefused):
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: draft_without_criteria,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-9",
            )

    def test_at_least_one_executable_assertion_among_several_suffices(self) -> None:
        signature_only = SuccessCriterionDraft(
            file=CAMPAIGN_LAUNCHER_PATH,
            subject="silent-timeout-swallow",
            required_paths=(CAMPAIGN_LAUNCHER_PATH,),
            statement="the swallow signature must no longer appear",
            assertion={"signature_absent": "silent-timeout-swallow"},
        )
        mixed_draft = _draft(
            success_criteria=(signature_only, _executable_criterion())
        )
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: mixed_draft,
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-10",
        )
        self.assertEqual(len(proposal.success_criteria), 2)

    def test_gate_forcing_path_without_gate_relaxation_risk_is_refused(self) -> None:
        gate_forcing_draft = _draft(
            target_surface=(TargetSurfaceDraft(path="AGENTS.md", kind="doc"),),
            rationale="touches AGENTS.md",
            evidence=(),
            accuracy_risk="none",
        )
        with self.assertRaises(ProposalRefused) as ctx:
            draft_proposal(
                _pattern(),
                judgment=lambda pattern: gate_forcing_draft,
                finding_history=_finding_history(),
                decision_registry=_decision_registry(),
                proposal_id="proposal-11",
            )
        self.assertIn("gate_relaxation", str(ctx.exception))


class RulingAcceptanceValidationTests(unittest.TestCase):
    """AC-SI04-3 (gate_relaxation naming) and AC-SI04-4 (human-authored)."""

    def _gate_relaxation_proposal(self):
        gate_draft = _draft(
            target_surface=(
                TargetSurfaceDraft(
                    path="scripts/dev/check_repository_contracts.py", kind="gate"
                ),
            ),
            rationale="relax the contracts gate; governed elsewhere",
            evidence=(),
            accuracy_risk="gate_relaxation",
        )
        return draft_proposal(
            _pattern(),
            judgment=lambda pattern: gate_draft,
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-gate",
        )

    def test_accept_ruling_requires_non_empty_actor_and_statement(self) -> None:
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-12",
        )
        with self.assertRaises(RulingError):
            apply_ruling(
                proposal,
                disposition="accept",
                actor="",
                statement="looks fine",
                ruled_at="2026-08-21T00:00:00Z",
            )
        with self.assertRaises(RulingError):
            apply_ruling(
                proposal,
                disposition="accept",
                actor="operator",
                statement="   ",
                ruled_at="2026-08-21T00:00:00Z",
            )
        self.assertEqual(proposal.status, "proposed")

    def test_accept_ruling_with_human_authored_fields_reaches_accepted(self) -> None:
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-13",
        )
        ruled = apply_ruling(
            proposal,
            disposition="accept",
            actor="operator",
            statement="Confirmed the fix is safe and reversible.",
            ruled_at="2026-08-21T00:00:00Z",
        )
        self.assertEqual(ruled.status, "accepted")
        self.assertEqual(ruled.ruling["actor"], "operator")
        # never mutates the input in place.
        self.assertEqual(proposal.status, "proposed")
        self.assertIsNone(proposal.ruling)

    def test_reject_and_waive_dispositions_do_not_reach_accepted(self) -> None:
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-14",
        )
        rejected = apply_ruling(
            proposal,
            disposition="reject",
            actor="operator",
            statement="not worth the churn right now",
            ruled_at="2026-08-21T00:00:00Z",
        )
        self.assertEqual(rejected.status, "rejected")

        waived = apply_ruling(
            proposal,
            disposition="waive",
            actor="operator",
            statement="accepted risk for now",
            ruled_at="2026-08-21T00:00:00Z",
        )
        self.assertEqual(waived.status, "rejected")
        self.assertEqual(waived.ruling["disposition"], "waive")

    def test_gate_relaxation_accept_rejects_statement_missing_gate_name(self) -> None:
        proposal = self._gate_relaxation_proposal()
        with self.assertRaises(RulingError) as ctx:
            apply_ruling(
                proposal,
                disposition="accept",
                actor="operator",
                statement="superseded by the new stricter check downstream",
                ruled_at="2026-08-21T00:00:00Z",
            )
        self.assertIn("does not name the relaxed gate", str(ctx.exception))

    def test_gate_relaxation_accept_rejects_statement_missing_superseding_mechanism(
        self,
    ) -> None:
        proposal = self._gate_relaxation_proposal()
        with self.assertRaises(RulingError) as ctx:
            apply_ruling(
                proposal,
                disposition="accept",
                actor="operator",
                statement="relaxes scripts/dev/check_repository_contracts.py, that's it",
                ruled_at="2026-08-21T00:00:00Z",
            )
        self.assertIn("superseding mechanism", str(ctx.exception))

    def test_gate_relaxation_accept_succeeds_when_gate_and_mechanism_are_named(
        self,
    ) -> None:
        proposal = self._gate_relaxation_proposal()
        ruled = apply_ruling(
            proposal,
            disposition="accept",
            actor="operator",
            statement=(
                "Relaxes scripts/dev/check_repository_contracts.py's strict "
                "timeout gate; superseded by the new retry-budget-ledger "
                "check enforcing the same floor downstream."
            ),
            ruled_at="2026-08-21T00:00:00Z",
        )
        self.assertEqual(ruled.status, "accepted")

    def test_gate_relaxation_reject_does_not_require_naming_the_mechanism(
        self,
    ) -> None:
        proposal = self._gate_relaxation_proposal()
        rejected = apply_ruling(
            proposal,
            disposition="reject",
            actor="operator",
            statement="not comfortable relaxing this gate yet",
            ruled_at="2026-08-21T00:00:00Z",
        )
        self.assertEqual(rejected.status, "rejected")

    def test_gate_relaxation_accept_rejects_when_no_gate_forcing_path_is_present(
        self,
    ) -> None:
        """A proposal can carry accuracy_risk 'gate_relaxation' with no
        gate-forcing target_surface entry (draft_proposal only refuses the
        converse). The ruling gate must still reject 'names the relaxed
        gate' rather than treating an empty gate list as vacuously
        satisfied."""

        gate_relaxation_without_a_gate = _draft(accuracy_risk="gate_relaxation")
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: gate_relaxation_without_a_gate,
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-vacuous-gate",
        )
        with self.assertRaises(RulingError) as ctx:
            apply_ruling(
                proposal,
                disposition="accept",
                actor="operator",
                statement="superseded by the new stricter check downstream",
                ruled_at="2026-08-21T00:00:00Z",
            )
        self.assertIn("no gate-forcing target_surface path", str(ctx.exception))

    def test_validate_gate_relaxation_ruling_helper_is_directly_callable(self) -> None:
        target_surface = [
            {"path": "AGENTS.md", "kind": "doc", "governing_decisions": []}
        ]
        with self.assertRaises(RulingError):
            validate_gate_relaxation_ruling(target_surface, "no mention of the doc")
        # no exception when both the gate and a superseding mechanism are named.
        validate_gate_relaxation_ruling(
            target_surface, "AGENTS.md is relaxed here; supersedes the old rule text"
        )
        with self.assertRaises(RulingError) as ctx:
            validate_gate_relaxation_ruling(
                [{"path": "harness_labs/graphrun/campaign_launcher.py", "kind": "code"}],
                "supersedes the old rule text",
            )
        self.assertIn("no gate-forcing target_surface path", str(ctx.exception))


class WireProjectionTests(unittest.TestCase):
    """The drafted ``Proposal.to_dict()`` conforms to
    ``improvement-proposal.schema.json`` under the SI-01 checker's own
    validation engine (``scripts/dev/check_improvement_artifacts.py``), not
    merely a key-presence check -- types, enum membership (``kind``,
    ``accuracy_risk``, ``status``), the ``assertion`` ``oneOf``, and
    ``additionalProperties`` are all exercised."""

    def test_to_dict_validates_cleanly_against_the_committed_checker(self) -> None:
        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-wire",
        )
        payload = proposal.to_dict()
        self.assertEqual(payload["protocol"], "improvement-proposal/1")
        self.assertEqual(
            checker.validate_artifact(payload, "proposal-wire"), []
        )
        self.assertIsNone(payload["ruling"])

        ruled = apply_ruling(
            proposal,
            disposition="accept",
            actor="operator",
            statement="Confirmed the fix is safe and reversible.",
            ruled_at="2026-08-21T00:00:00Z",
        )
        ruled_payload = ruled.to_dict()
        self.assertEqual(ruled_payload["status"], "accepted")
        self.assertEqual(
            set(ruled_payload["ruling"]),
            {"disposition", "actor", "statement", "ruled_at"},
        )
        self.assertEqual(
            checker.validate_artifact(ruled_payload, "proposal-wire-ruled"), []
        )

    def test_checker_actually_catches_an_invalid_wire_payload(self) -> None:
        """Proves the gate proves conformance rather than asserting it: a
        payload with an out-of-enum ``kind`` and a required field missing
        must be rejected by the same checker call the prior test relies on
        returning ``[]``."""

        proposal = draft_proposal(
            _pattern(),
            judgment=lambda pattern: _draft(),
            finding_history=_finding_history(),
            decision_registry=_decision_registry(),
            proposal_id="proposal-wire-invalid",
        )
        payload = proposal.to_dict()
        payload["target_surface"][0]["kind"] = "not-a-real-kind"
        del payload["rollback"]
        errors = checker.validate_artifact(payload, "proposal-wire-invalid")
        self.assertTrue(errors)
        self.assertTrue(any("not-a-real-kind" in error for error in errors))
        self.assertTrue(any("rollback" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
