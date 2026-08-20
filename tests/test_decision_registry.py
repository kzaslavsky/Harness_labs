"""Tests for harness_labs/core/decision_registry.py (EM-C2 of the
engineering-memory port).

All fixtures are built inside tmp_path; nothing here depends on the live
count of ADRs under docs/decisions/ (containment assertions only, per
AC-EM-11).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness_labs.core.decision_registry import (
    Decision,
    DecisionRegistryError,
    load_decisions,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


ADR_0001 = """# 0001 — First decision

Status: accepted
Concerns-paths: harness_labs/plangraph/plan_graph.py, harness_labs/plangraph/plan_graph_join.py
Date: 2026-08-10
Owners: harness controller

## Context

Body text.
"""

ADR_0007_WRAPPED_RUN = """# 0007 — Escalation with bounded unsealing

Status: accepted
Concerns-paths: harness_labs/featurerun/review_fix.py, harness_labs/plangraph/plan_graph.py
Date: 2026-08-19
Owners: PlanGraph controller
Run: `cc-graph/convergence-campaign-harness` (attempts 1-3), logical graph
`convergence-campaign-harness`
Valid-from-commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

## Context

Body text.
"""


def _covers_by_prefix(concerns_path: str, queried_path: str) -> bool:
    """Mirrors the segment-wise prefix check in decision_registry.py so
    tests can derive expected coverage from loaded decisions rather than
    hard-coding ids (AC-EM-11)."""

    concerns_parts = [p for p in concerns_path.split("/") if p]
    queried_parts = [p for p in queried_path.split("/") if p]
    if len(concerns_parts) > len(queried_parts):
        return False
    return queried_parts[: len(concerns_parts)] == concerns_parts


class TmpDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


class LoadDecisionsAdrTests(TmpDirTestCase):
    def test_two_files_numbered_0006_load_as_distinct_decisions(self) -> None:
        _write(
            self.tmp_path / "0006-parallel-plangraph-contract.md",
            "# 0006 — Parallel PlanGraph contract\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/plangraph/plan_graph.py\n\n"
            "## Context\n\nBody.\n",
        )
        _write(
            self.tmp_path / "0006-repository-bound-plan-approval.md",
            "# 0006 — Repository-bound PlanGraph approval\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/plangraph/plan_approval.py\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        by_id = {decision.id: decision for decision in registry.decisions}
        self.assertIn("0006-parallel-plangraph-contract", by_id)
        self.assertIn("0006-repository-bound-plan-approval", by_id)
        self.assertEqual(len(by_id), 2)
        self.assertEqual(
            by_id["0006-parallel-plangraph-contract"].status, "accepted"
        )
        self.assertEqual(
            by_id["0006-repository-bound-plan-approval"].status, "accepted"
        )

    def test_wrapped_run_header_does_not_corrupt_adjacent_fields(self) -> None:
        _write(self.tmp_path / "0007-escalation.md", ADR_0007_WRAPPED_RUN)
        registry = load_decisions(self.tmp_path)
        (decision,) = registry.decisions
        self.assertEqual(decision.status, "accepted")
        self.assertEqual(
            decision.concerns_paths,
            (
                "harness_labs/featurerun/review_fix.py",
                "harness_labs/plangraph/plan_graph.py",
            ),
        )
        # The wrapped Run: value folds onto one field and does not bleed
        # into Concerns-paths/Status, and no spurious "Run" or backtick-only
        # field corrupts decision identity or status.
        self.assertEqual(decision.id, "0007-escalation")
        # Valid-from-commit follows the wrapped Run: continuation line; if
        # the continuation bled past Run into this field (or swallowed it),
        # this would come back None or corrupted instead of the fixture value.
        self.assertEqual(
            decision.valid_from_commit, "b" * 40
        )

    def test_decision_dataclass_carries_exactly_the_specified_fields(self) -> None:
        _write(self.tmp_path / "0001-first.md", ADR_0001)
        registry = load_decisions(self.tmp_path)
        (decision,) = registry.decisions
        self.assertIsInstance(decision, Decision)
        field_names = {f for f in decision.__dataclass_fields__}
        self.assertEqual(
            field_names,
            {
                "id",
                "status",
                "supersedes",
                "concerns_paths",
                "valid_from_commit",
                "source_path",
            },
        )
        self.assertEqual(decision.valid_from_commit, None)
        self.assertTrue(decision.source_path.endswith("0001-first.md"))

    def test_supersedes_header_parses_to_a_tuple(self) -> None:
        _write(
            self.tmp_path / "0002-second.md",
            "# 0002 — Second\n\n"
            "Status: accepted\n"
            "Supersedes: 0001-first\n"
            "Concerns-paths: harness_labs/core/x.py\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        (decision,) = registry.decisions
        self.assertEqual(decision.supersedes, ("0001-first",))

    def test_non_adr_non_json_files_are_ignored(self) -> None:
        _write(self.tmp_path / "README.md", "# Not an ADR\n\nplain prose\n")
        _write(self.tmp_path / "TEMPLATE.md", "# NNNN — Title\n\nStatus: proposed\n")
        _write(self.tmp_path / "notes.txt", "irrelevant\n")
        registry = load_decisions(self.tmp_path)
        self.assertEqual(registry.decisions, ())


class LoadDecisionsJsonTests(TmpDirTestCase):
    def _json_record(self, **overrides: object) -> dict[str, object]:
        record = {
            "schema_version": "1.1",
            "decision_id": "json-decision-1",
            "run_id": "run-1",
            "timestamp": "2026-08-20T00:00:00Z",
            "actor_id": "actor-1",
            "phase": "implementation",
            "question": "Which approach?",
            "choice": "approach A",
            "alternatives": [],
            "rationale": "simplicity",
            "evidence": [],
            "consequences": [],
            "reversible": True,
            "status": "accepted",
            "supersedes": [],
            "concerns_paths": ["harness_labs/core/decision_registry.py"],
            "valid_from_commit": "a" * 40,
        }
        record.update(overrides)
        return record

    def test_schema_1_1_json_record_loads_into_a_decision(self) -> None:
        _write(
            self.tmp_path / "record.json",
            json.dumps(self._json_record()),
        )
        registry = load_decisions(self.tmp_path)
        (decision,) = registry.decisions
        self.assertEqual(decision.id, "json-decision-1")
        self.assertEqual(decision.status, "accepted")
        self.assertEqual(
            decision.concerns_paths, ("harness_labs/core/decision_registry.py",)
        )
        self.assertEqual(decision.valid_from_commit, "a" * 40)

    def test_schema_1_0_json_record_without_lifecycle_fields_loads(self) -> None:
        record = self._json_record(schema_version="1.0")
        del record["supersedes"]
        del record["concerns_paths"]
        del record["valid_from_commit"]
        _write(self.tmp_path / "record.json", json.dumps(record))
        registry = load_decisions(self.tmp_path)
        (decision,) = registry.decisions
        self.assertEqual(decision.concerns_paths, ())
        self.assertEqual(decision.valid_from_commit, None)

    def test_controller_kernel_shaped_record_is_not_ingested(self) -> None:
        # harness_labs.core.controller_kernel._decision_record's shape: no
        # schema_version, "id" instead of "decision_id", and fields
        # ("question", "choice", "alternatives", "rationale",
        # "evidence_refs", "actor") that do not match decision.schema.json.
        kernel_shaped = {
            "id": "kernel-decision-1",
            "question": "Which approach?",
            "choice": "approach A",
            "alternatives": ["approach B"],
            "rationale": "simplicity",
            "evidence_refs": ["tests/test_decision_registry.py"],
            "actor": {"id": "actor-1", "role": "controller"},
        }
        _write(self.tmp_path / "kernel_record.json", json.dumps(kernel_shaped))
        registry = load_decisions(self.tmp_path)
        self.assertEqual(registry.decisions, ())

    def test_malformed_json_file_is_skipped_not_raised(self) -> None:
        _write(self.tmp_path / "broken.json", "{not valid json")
        registry = load_decisions(self.tmp_path)
        self.assertEqual(registry.decisions, ())

    def test_non_dict_json_file_is_skipped(self) -> None:
        _write(self.tmp_path / "list.json", json.dumps([1, 2, 3]))
        registry = load_decisions(self.tmp_path)
        self.assertEqual(registry.decisions, ())

    def test_json_record_with_non_string_supersedes_raises(self) -> None:
        record = self._json_record(supersedes=[1, 2])
        _write(self.tmp_path / "record.json", json.dumps(record))
        with self.assertRaises(DecisionRegistryError):
            load_decisions(self.tmp_path)


class ActiveDecisionsForPathsTests(TmpDirTestCase):
    def test_directory_prefix_intersection(self) -> None:
        _write(
            self.tmp_path / "0001-covers-plangraph.md",
            "# 0001 — Covers plangraph\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/plangraph\n\n"
            "## Context\n\nBody.\n",
        )
        _write(
            self.tmp_path / "0002-covers-featurerun.md",
            "# 0002 — Covers featurerun\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/featurerun\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        result = registry.active_decisions_for_paths(
            ("harness_labs/plangraph/plan_graph.py",)
        )
        active_ids = {decision.id for decision in result.active}
        self.assertIn("0001-covers-plangraph", active_ids)
        self.assertNotIn("0002-covers-featurerun", active_ids)

    def test_exact_prefix_segment_match_not_substring_match(self) -> None:
        # "harness_labs/plangraph" must not spuriously cover
        # "harness_labs/plangraph_extra/x.py" (segment-wise, not raw
        # string-prefix, comparison).
        _write(
            self.tmp_path / "0001-covers-plangraph.md",
            "# 0001 — Covers plangraph\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/plangraph\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        result = registry.active_decisions_for_paths(
            ("harness_labs/plangraph_extra/x.py",)
        )
        self.assertEqual(result.active, ())

    def test_only_accepted_status_decisions_are_active(self) -> None:
        _write(
            self.tmp_path / "0001-proposed.md",
            "# 0001 — Proposed\n\n"
            "Status: proposed\n"
            "Concerns-paths: harness_labs/core\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        result = registry.active_decisions_for_paths(("harness_labs/core/x.py",))
        self.assertEqual(result.active, ())

    def test_cleanly_superseded_decision_is_excluded_without_inconsistency(
        self,
    ) -> None:
        _write(
            self.tmp_path / "0001-old.md",
            "# 0001 — Old\n\n"
            "Status: superseded\n"
            "Concerns-paths: harness_labs/core\n\n"
            "## Context\n\nBody.\n",
        )
        _write(
            self.tmp_path / "0002-new.md",
            "# 0002 — New\n\n"
            "Status: accepted\n"
            "Supersedes: 0001-old\n"
            "Concerns-paths: harness_labs/core\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        result = registry.active_decisions_for_paths(("harness_labs/core/x.py",))
        active_ids = {decision.id for decision in result.active}
        self.assertEqual(active_ids, {"0002-new"})
        self.assertEqual(result.inconsistencies, ())

    def test_accepted_vs_supersedes_contradiction_is_surfaced(self) -> None:
        _write(
            self.tmp_path / "0001-x.md",
            "# 0001 — X\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/core\n\n"
            "## Context\n\nBody.\n",
        )
        _write(
            self.tmp_path / "0002-y.md",
            "# 0002 — Y\n\n"
            "Status: accepted\n"
            "Supersedes: 0001-x\n"
            "Concerns-paths: harness_labs/core\n\n"
            "## Context\n\nBody.\n",
        )
        registry = load_decisions(self.tmp_path)
        result = registry.active_decisions_for_paths(("harness_labs/core/x.py",))

        self.assertEqual(len(result.inconsistencies), 1)
        inconsistency = result.inconsistencies[0]
        self.assertEqual(inconsistency.superseded_id, "0001-x")
        self.assertEqual(inconsistency.superseding_id, "0002-y")

        # X is contradictory, not silently resolved either way: it is
        # excluded from active, and — unlike the cleanly-superseded case
        # (test_cleanly_superseded_decision_is_excluded_without_inconsistency,
        # which surfaces zero inconsistencies) — its exclusion here is
        # accompanied by the Inconsistency asserted above, so it is never
        # dropped without a surfaced trace of why. Y remains active,
        # showing the contradiction does not poison unrelated decisions.
        active_ids = {decision.id for decision in result.active}
        self.assertEqual(active_ids, {"0002-y"})

    def test_no_inconsistency_when_paths_unrelated(self) -> None:
        registry = load_decisions(self.tmp_path)
        result = registry.active_decisions_for_paths(("harness_labs/core/x.py",))
        self.assertEqual(result.active, ())
        self.assertEqual(result.inconsistencies, ())


class LoadDecisionsFromRealDocsDecisionsTests(unittest.TestCase):
    """Runs against the real docs/decisions/ tree, but only asserts
    containment — never set equality against the live ADR count
    (AC-EM-11)."""

    def test_load_decisions_over_docs_decisions_contains_known_adrs(self) -> None:
        root = Path(__file__).resolve().parent.parent / "docs" / "decisions"
        registry = load_decisions(root)
        by_id = {decision.id: decision for decision in registry.decisions}
        self.assertIn("0006-parallel-plangraph-contract", by_id)
        self.assertIn("0006-repository-bound-plan-approval", by_id)
        self.assertIn("0007-in-graph-escalation-bounded-unsealing", by_id)
        # Both files numbered 0006 must load as distinct *accepted* decisions.
        self.assertEqual(
            by_id["0006-parallel-plangraph-contract"].status, "accepted"
        )
        self.assertEqual(
            by_id["0006-repository-bound-plan-approval"].status, "accepted"
        )
        # TEMPLATE.md/README.md are not numbered ADRs and must not appear.
        self.assertNotIn("TEMPLATE", by_id)
        self.assertNotIn("README", by_id)

    def test_active_decisions_for_plan_graph_contains_expected_adrs(self) -> None:
        root = Path(__file__).resolve().parent.parent / "docs" / "decisions"
        registry = load_decisions(root)
        target_path = "harness_labs/plangraph/plan_graph.py"

        # Derive the expected set from the loaded decisions themselves
        # (every accepted decision whose concerns_paths cover target_path
        # by directory prefix), rather than hard-coding ids: the gate must
        # stay red if active_decisions_for_paths silently drops a
        # qualifying decision, whatever the live ADR set happens to be.
        expected_ids = {
            decision.id
            for decision in registry.decisions
            if decision.status == "accepted"
            and any(
                _covers_by_prefix(concerns_path, target_path)
                for concerns_path in decision.concerns_paths
            )
        }
        self.assertIn("0006-parallel-plangraph-contract", expected_ids)
        self.assertIn("0007-in-graph-escalation-bounded-unsealing", expected_ids)

        result = registry.active_decisions_for_paths((target_path,))
        active_ids = {decision.id for decision in result.active}
        for expected_id in expected_ids:
            self.assertIn(expected_id, active_ids)
        for decision in result.active:
            self.assertEqual(decision.status, "accepted")


if __name__ == "__main__":
    unittest.main()
