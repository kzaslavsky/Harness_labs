from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_labs.observability import run_forensics
from harness_labs.observability.run_forensics import mine


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CORPUS = REPO_ROOT / "tests" / "fixtures" / "improvement" / "journals" / "corpus"
CHECKER_PATH = REPO_ROOT / "scripts" / "dev" / "check_improvement_artifacts.py"

# SI-01's stdlib-only schema checker, loaded the same way
# tests/test_improvement_artifacts.py does: every emitted observation must
# validate against blocker-observation.schema.json, not just look plausible.
_checker_spec = importlib.util.spec_from_file_location("check_improvement_artifacts", CHECKER_PATH)
checker = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(checker)

_ABS_PATH_MARKER = "/Users/"
_TIMESTAMP_MARKER = "2026-08-01T00:00:05"
_VALID_RUN_ID = "run-si02-valid-001"
_VALID_RUN_ID_2 = "run-si02-valid-002"
_CAUSES_RUN_ID = "run-si02-causes-001"
_LIFECYCLE_RUN_ID = "run-si02-lifecycle-001"
_FABRICATED_RUN_ID = "run-si02-fabricated-001"
_TAMPERED_RUN_DIR = "run-si02-tampered-001"
_PRODUCTION_RUN_IDS = (_VALID_RUN_ID, _VALID_RUN_ID_2, _CAUSES_RUN_ID, _LIFECYCLE_RUN_ID)
_KNOWN_RUN_IDS = _PRODUCTION_RUN_IDS + (_FABRICATED_RUN_ID, _TAMPERED_RUN_DIR)


class _CorpusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.runs_root = root / "runs"
        self.state_root = root / "state"
        shutil.copytree(FIXTURE_CORPUS, self.runs_root)

    def _observations_for(self, result, run_id: str) -> list[dict]:
        return [obs for obs in result.observations if obs["run_id"] == run_id]


class HashChainAdmissionTests(_CorpusTestCase):
    """AC-SI02-1: admission is gated on audit chain verification via project_run_metrics."""

    def test_tampered_journal_is_refused_not_silently_skipped(self) -> None:
        result = mine(self.runs_root, state_root=self.state_root)
        refused_dirs = [refusal.run_dir for refusal in result.refused]
        self.assertIn(_TAMPERED_RUN_DIR, refused_dirs)

    def test_tampered_journal_refusal_names_the_hash_mismatch_cause(self) -> None:
        """The tampered fixture is tampered by rewriting one event's payload
        text in place without recomputing its event_hash (see
        tests/fixtures' generator): the refusal reason must name that exact
        cause, not just be any non-empty string that would equally match a
        missing-file or unreadable-JSON refusal."""

        result = mine(self.runs_root, state_root=self.state_root)
        tampered = [refusal for refusal in result.refused if refusal.run_dir == _TAMPERED_RUN_DIR]
        self.assertEqual(len(tampered), 1)
        reason = tampered[0].reason.lower()
        self.assertIn("hash", reason)
        self.assertIn("does not match", reason)

    def test_tampered_journal_contributes_no_observation(self) -> None:
        result = mine(self.runs_root, state_root=self.state_root)
        for observation in result.observations:
            self.assertNotEqual(observation["run_id"], _TAMPERED_RUN_DIR)

    def test_verified_runs_are_not_refused(self) -> None:
        result = mine(self.runs_root, state_root=self.state_root)
        refused_dirs = {refusal.run_dir for refusal in result.refused}
        self.assertNotIn(_VALID_RUN_ID, refused_dirs)
        self.assertNotIn(_FABRICATED_RUN_ID, refused_dirs)


class ObservationExtractionTests(_CorpusTestCase):
    """AC-SI02-2: the four observation kinds are emitted with normalized signatures."""

    def setUp(self) -> None:
        super().setUp()
        self.result = mine(self.runs_root, state_root=self.state_root)
        self.valid_observations = self._observations_for(self.result, _VALID_RUN_ID)

    def test_retry_event_observation_emitted(self) -> None:
        phases = {obs["phase"] for obs in self.valid_observations}
        self.assertIn("retry", phases)

    def test_failed_status_observation_emitted(self) -> None:
        phases = {obs["phase"] for obs in self.valid_observations}
        self.assertIn("failed", phases)

    def test_blocked_status_observation_emitted(self) -> None:
        phases = {obs["phase"] for obs in self.valid_observations}
        self.assertIn("blocked", phases)

    def test_review_ledger_reopened_finding_observation_emitted(self) -> None:
        phases = {obs["phase"] for obs in self.valid_observations}
        self.assertIn("review_reopened", phases)
        # exactly one finding in the fixture ledger has reopened_count > 0
        reopened = [obs for obs in self.valid_observations if obs["phase"] == "review_reopened"]
        self.assertEqual(len(reopened), 1)

    def test_retry_budget_abandoned_and_extended_observations_emitted(self) -> None:
        phases = {obs["phase"] for obs in self.valid_observations}
        self.assertIn("retry_budget_abandoned", phases)
        self.assertIn("retry_budget_extended", phases)

    def test_every_signature_is_secret_free_and_normalized(self) -> None:
        """Independent of the implementation's own regexes: checks every
        *known* run id (not just each observation's own run_id, since a
        foreign run id quoted in another run's free text is exactly the
        class of leak the implementation used to miss) and a bare-date
        pattern that is strictly broader than the implementation's own
        full-timestamp regex, so this cannot pass merely by mirroring
        whatever the implementation happens to strip."""

        self.assertGreater(len(self.result.observations), 0)
        for observation in self.result.observations:
            signature = observation["signature"]
            self.assertNotIn(_ABS_PATH_MARKER, signature)
            for run_id in _KNOWN_RUN_IDS:
                self.assertNotIn(run_id, signature, msg=f"run id {run_id!r} survived in signature: {signature!r}")
            self.assertNotRegex(
                signature,
                r"\d{4}-\d{2}-\d{2}",
                msg=f"date/timestamp survived in signature: {signature!r}",
            )

    def test_every_observation_validates_against_the_blocker_observation_schema(self) -> None:
        """SI-01 shipped blocker-observation.schema.json plus a stdlib-only
        validator; a record missing a required field or carrying an
        out-of-enum value must fail this gate, not just look plausible."""

        self.assertGreater(len(self.result.observations), 0)
        for observation in self.result.observations:
            self.assertEqual(observation["protocol"], "blocker-observation/1")
            errors = checker.validate_artifact(observation, observation.get("signature", "observation"))
            self.assertEqual(errors, [], msg=f"schema violations: {errors}")

    def test_normalizer_strips_absolute_path_run_id_and_timestamp(self) -> None:
        raw = (
            "assertion failed for run-si02-valid-001 at /Users/example/project/tests/test_thing.py:42 "
            "on 2026-08-01T00:00:05Z"
        )
        normalized = run_forensics.normalize_signature_text(
            raw, strip_literal=("run-si02-valid-001",)
        )
        self.assertNotIn("/Users/example", normalized)
        self.assertNotIn("run-si02-valid-001", normalized)
        self.assertNotIn("2026-08-01T00:00:05Z", normalized)


class ProductionLifecycleFilterTests(_CorpusTestCase):
    """AC-SI02-3: non-production_lifecycle runs are parsed but excluded from every aggregate."""

    def test_fabricated_fixture_run_is_admitted_and_excluded(self) -> None:
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertIn(_FABRICATED_RUN_ID, result.excluded_run_ids)
        fabricated_dirs = {refusal.run_dir for refusal in result.refused}
        self.assertNotIn(_FABRICATED_RUN_ID, fabricated_dirs)

    def test_fabricated_fixture_run_contributes_zero_observations(self) -> None:
        result = mine(self.runs_root, state_root=self.state_root)
        fabricated_observations = self._observations_for(result, _FABRICATED_RUN_ID)
        self.assertEqual(len(fabricated_observations), 0)

    def test_aggregate_count_unaffected_by_excluded_and_refused_runs(self) -> None:
        """Mining the full mixed corpus yields the same production observation
        count as mining only the production_lifecycle run directories -- the
        fabricated_fixture and tampered runs contribute nothing either way."""

        mixed_result = mine(self.runs_root, state_root=self.state_root)
        mixed_production_count = len(
            [obs for obs in mixed_result.observations if obs["evidence_classification"] == "production_lifecycle"]
        )

        production_only_root = Path(self._tmp.name) / "production_only"
        production_only_root.mkdir()
        for run_id in _PRODUCTION_RUN_IDS:
            shutil.copytree(self.runs_root / run_id, production_only_root / run_id)
        production_only_state = Path(self._tmp.name) / "production_only_state"
        production_only_result = mine(production_only_root, state_root=production_only_state)

        self.assertEqual(mixed_production_count, len(production_only_result.observations))
        self.assertGreater(mixed_production_count, 0)

    def test_every_observation_in_aggregate_is_production_lifecycle(self) -> None:
        result = mine(self.runs_root, state_root=self.state_root)
        classifications = {obs["evidence_classification"] for obs in result.observations}
        self.assertEqual(classifications, {"production_lifecycle"})


class WatermarkIdempotenceTests(unittest.TestCase):
    """AC-SI02-4: mining is watermarked and idempotent under a state root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runs_root = self.root / "runs"
        self.state_root = self.root / "state"
        self.runs_root.mkdir()
        shutil.copytree(
            FIXTURE_CORPUS / _VALID_RUN_ID, self.runs_root / _VALID_RUN_ID
        )

    def test_second_run_over_unchanged_corpus_emits_no_new_observations(self) -> None:
        first = mine(self.runs_root, state_root=self.state_root)
        self.assertGreater(len(first.observations), 0)
        self.assertIn(_VALID_RUN_ID, first.new_run_dirs)

        second = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(second.observations, ())
        self.assertEqual(second.new_run_dirs, ())
        self.assertEqual(second.excluded_run_ids, ())
        self.assertEqual(second.refused, ())

    def test_adding_one_new_run_directory_mines_only_that_run(self) -> None:
        first = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(first.new_run_dirs, (_VALID_RUN_ID,))

        shutil.copytree(
            FIXTURE_CORPUS / _VALID_RUN_ID_2, self.runs_root / _VALID_RUN_ID_2
        )
        second = mine(self.runs_root, state_root=self.state_root)

        self.assertEqual(second.new_run_dirs, (_VALID_RUN_ID_2,))
        self.assertTrue(all(obs["run_id"] == _VALID_RUN_ID_2 for obs in second.observations))
        self.assertGreater(len(second.observations), 0)

    def test_watermark_state_persists_to_state_root(self) -> None:
        mine(self.runs_root, state_root=self.state_root)
        state_file = self.state_root / run_forensics.STATE_FILENAME
        self.assertTrue(state_file.is_file())
        self.assertIn(_VALID_RUN_ID, state_file.read_text(encoding="utf-8"))

    def test_mining_is_deterministic_across_repeated_fresh_runs(self) -> None:
        """Two independent mine() calls over the same fresh corpus (distinct
        state roots) produce byte-identical observation signatures/ordering --
        no wall-clock or other nondeterminism leaks into output."""

        state_a = self.root / "state_a"
        state_b = self.root / "state_b"
        result_a = mine(self.runs_root, state_root=state_a)
        result_b = mine(self.runs_root, state_root=state_b)
        self.assertEqual(result_a.observations, result_b.observations)


class NestedGraphRootDiscoveryTests(unittest.TestCase):
    """DEFECT 1: PlanGraph campaigns nest runs one level below the runs root
    (``logs/runs/<graph-root>/<run-id>/``). A miner that only ever reads
    immediate children of the runs root mined zero observations over a real
    campaign and reported no reason for the emptiness."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runs_root = self.root / "runs"
        self.state_root = self.root / "state"
        self.runs_root.mkdir()

    def _nest(self, graph_root: str, run_id: str, source: str | None = None) -> Path:
        destination = self.runs_root / graph_root / run_id
        shutil.copytree(FIXTURE_CORPUS / (source or run_id), destination)
        return destination

    def test_runs_nested_under_a_graph_root_are_mined(self) -> None:
        self._nest("si-graph", _VALID_RUN_ID)
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertGreater(len(result.observations), 0)
        self.assertTrue(all(obs["run_id"] == _VALID_RUN_ID for obs in result.observations))

    def test_nested_run_yields_the_same_observations_as_a_flat_run(self) -> None:
        """The nesting is a directory-layout detail, not a data difference:
        mining the same journal flat or one level down must produce the same
        observations."""

        self._nest("si-graph", _VALID_RUN_ID)
        nested = mine(self.runs_root, state_root=self.state_root)

        flat_root = self.root / "flat"
        flat_root.mkdir()
        shutil.copytree(FIXTURE_CORPUS / _VALID_RUN_ID, flat_root / _VALID_RUN_ID)
        flat = mine(flat_root, state_root=self.root / "flat_state")

        self.assertEqual(nested.observations, flat.observations)

    def test_watermark_key_of_a_flat_run_is_still_its_bare_directory_name(self) -> None:
        """Runs already watermarked by the previous release must stay
        watermarked; their key may not silently change shape."""

        shutil.copytree(FIXTURE_CORPUS / _VALID_RUN_ID, self.runs_root / _VALID_RUN_ID)
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(result.new_run_dirs, (_VALID_RUN_ID,))
        state = json.loads((self.state_root / run_forensics.STATE_FILENAME).read_text(encoding="utf-8"))
        self.assertIn(_VALID_RUN_ID, state["processed_run_dirs"])

    def test_same_named_runs_under_two_graph_roots_do_not_share_a_watermark(self) -> None:
        """Two graph roots may hold identically named run directories; a
        watermark keyed on the bare name alone would seal the second run
        the moment the first was mined."""

        self._nest("graph-a", _VALID_RUN_ID)
        self._nest("graph-b", _VALID_RUN_ID)
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(
            result.new_run_dirs,
            (f"graph-a/{_VALID_RUN_ID}", f"graph-b/{_VALID_RUN_ID}"),
        )
        state = json.loads((self.state_root / run_forensics.STATE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(state["processed_run_dirs"]),
            [f"graph-a/{_VALID_RUN_ID}", f"graph-b/{_VALID_RUN_ID}"],
        )

    def test_nested_mining_is_still_watermarked_and_idempotent(self) -> None:
        self._nest("si-graph", _VALID_RUN_ID)
        first = mine(self.runs_root, state_root=self.state_root)
        self.assertGreater(len(first.observations), 0)
        second = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(second.observations, ())
        self.assertEqual(second.new_run_dirs, ())

    def test_flat_and_nested_runs_coexist_under_one_root(self) -> None:
        shutil.copytree(FIXTURE_CORPUS / _VALID_RUN_ID, self.runs_root / _VALID_RUN_ID)
        self._nest("si-graph", _VALID_RUN_ID_2)
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(
            result.new_run_dirs, (_VALID_RUN_ID, f"si-graph/{_VALID_RUN_ID_2}")
        )

    def test_descent_is_bounded_at_one_level(self) -> None:
        """A run buried two levels deep is not mined -- and is reported as
        skipped rather than silently ignored."""

        deep = self.runs_root / "outer" / "inner" / _VALID_RUN_ID
        shutil.copytree(FIXTURE_CORPUS / _VALID_RUN_ID, deep)
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(result.observations, ())
        self.assertEqual(result.new_run_dirs, ())
        skipped = {entry.path: entry.reason for entry in result.skipped}
        self.assertIn("outer", skipped)

    def test_a_root_of_only_containers_explains_its_empty_harvest(self) -> None:
        """The real-world regression: every direct child of ``logs/runs`` is
        a graph root, so the old miner reported observation_count 0 with no
        coverage explanation and the scheduled audit no-opped forever."""

        (self.runs_root / "not-a-run").mkdir()
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(result.observations, ())
        self.assertEqual([entry.path for entry in result.skipped], ["not-a-run"])
        self.assertIn(
            run_forensics.RUN_JOURNAL_FILENAME, result.skipped[0].reason
        )

    def test_non_run_children_of_a_container_are_reported_skipped(self) -> None:
        self._nest("si-graph", _VALID_RUN_ID)
        (self.runs_root / "si-graph" / "verification-scratch").mkdir()
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertIn(
            "si-graph/verification-scratch", [entry.path for entry in result.skipped]
        )

    def test_dotted_bookkeeping_directories_are_skipped_with_a_reason(self) -> None:
        """``.plan-graph-budgets`` / ``.plan-graph-locks`` sit beside runs.
        The dashboard catalog excludes them; the miner must too -- but must
        say so rather than drop them into an unexplained gap."""

        shutil.copytree(FIXTURE_CORPUS / _VALID_RUN_ID, self.runs_root / _VALID_RUN_ID)
        (self.runs_root / ".plan-graph-budgets").mkdir()
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertNotIn(".plan-graph-budgets", result.new_run_dirs)
        self.assertNotIn(".plan-graph-budgets", [refusal.run_dir for refusal in result.refused])
        skipped = {entry.path: entry.reason for entry in result.skipped}
        self.assertIn(".plan-graph-budgets", skipped)
        self.assertIn("bookkeeping", skipped[".plan-graph-budgets"])

    def test_missing_runs_root_is_explained_rather_than_silently_empty(self) -> None:
        result = mine(self.root / "absent", state_root=self.state_root)
        self.assertEqual(result.observations, ())
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("not a directory", result.skipped[0].reason)

    def test_a_nested_tampered_run_is_still_refused_under_its_qualified_key(self) -> None:
        self._nest("si-graph", _TAMPERED_RUN_DIR)
        result = mine(self.runs_root, state_root=self.state_root)
        self.assertEqual(
            [refusal.run_dir for refusal in result.refused],
            [f"si-graph/{_TAMPERED_RUN_DIR}"],
        )


class CauseShapedExtractionTests(_CorpusTestCase):
    """DEFECT 2: classification and signature come from the strongest
    node-level source available, not from the lifecycle event name."""

    def setUp(self) -> None:
        super().setUp()
        self.result = mine(self.runs_root, state_root=self.state_root)
        self.causes = self._observations_for(self.result, _CAUSES_RUN_ID)
        self.signatures = {obs["signature"] for obs in self.causes}

    def test_command_rejection_signature_is_the_error_code_not_the_event_name(self) -> None:
        self.assertIn("command_rejected:unknown_evidence", self.signatures)
        self.assertNotIn("failed:command_rejected", self.signatures)

    def test_command_rejection_rule_id_keeps_the_command_type(self) -> None:
        """The coarse signature clusters; the closed schema's existing
        rule_id field is where the finer identity survives."""

        rule_ids = {
            obs["rule_id"]
            for obs in self.causes
            if obs["signature"] == "command_rejected:unknown_evidence"
        }
        self.assertEqual(
            rule_ids,
            {
                "command_rejected:run.complete_request:unknown_evidence",
                "command_rejected:task.dispatch:unknown_evidence",
            },
        )

    def test_command_rejection_is_classified_not_indeterminate(self) -> None:
        classifications = {
            obs["classification"]
            for obs in self.causes
            if obs["signature"] == "command_rejected:unknown_evidence"
        }
        self.assertEqual(classifications, {"harness_or_configuration"})

    def test_deliverable_floor_reason_becomes_the_cause(self) -> None:
        floor = [obs for obs in self.causes if obs["signature"] == "deliverable_floor:placeholder_token"]
        self.assertEqual(len(floor), 1)
        self.assertEqual(floor[0]["classification"], "policy_violation")

    def test_bypassed_gate_slot_is_a_harness_cause(self) -> None:
        bypassed = [obs for obs in self.causes if obs["signature"] == "gate_slot_bypassed"]
        self.assertEqual(len(bypassed), 1)
        self.assertEqual(bypassed[0]["classification"], "harness_or_configuration")

    def test_required_findings_stop_is_a_named_cause(self) -> None:
        findings = [obs for obs in self.causes if obs["signature"] == "required_findings_open"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "policy_violation")

    def test_block_escalation_node_classification_is_taken_verbatim(self) -> None:
        """``plan-graph-block-escalation/1`` carries the 6-value enum per
        node; that is a far stronger source than the lifecycle event that
        merely points at the artifact."""

        escalated = [obs for obs in self.causes if obs["phase"] == "block_escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0]["classification"], "harness_or_configuration")
        self.assertEqual(escalated[0]["node_id"], "SI-05")
        self.assertEqual(escalated[0]["signature"], "worker_completed_without_change")

    def test_succeeded_nodes_in_an_escalation_mint_no_observation(self) -> None:
        escalated_nodes = {obs["node_id"] for obs in self.causes if obs["phase"] == "block_escalated"}
        self.assertNotIn("SI-04", escalated_nodes)

    def test_escalated_review_finding_is_mined_with_its_escalation_reason(self) -> None:
        escalated = [obs for obs in self.causes if obs["phase"] == "review_escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0]["signature"], "review_fix:no_progress")
        self.assertEqual(escalated[0]["rule_id"], "escalated-1")
        self.assertEqual(escalated[0]["classification"], "product")

    def test_contract_violating_review_finding_is_a_policy_violation(self) -> None:
        reopened = [obs for obs in self.causes if obs["phase"] == "review_reopened"]
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]["classification"], "policy_violation")
        self.assertEqual(reopened[0]["rule_id"], "contract-1")

    def test_retry_budget_signature_is_built_from_failure_keys(self) -> None:
        """AC: cause-shaped keys (here the ledger's own ``failure_keys``)
        are preferred over the free-text reason."""

        abandoned = [
            obs
            for obs in self._observations_for(self.result, _VALID_RUN_ID)
            if obs["phase"] == "retry_budget_abandoned"
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]["signature"], "retry_budget_abandoned:gate:verification-timeout")
        self.assertEqual(abandoned[0]["rule_id"], "gate:verification-timeout")

    def test_verification_rule_id_still_wins_over_every_weaker_source(self) -> None:
        failed = [
            obs
            for obs in self._observations_for(self.result, _VALID_RUN_ID)
            if obs["rule_id"] == "product-assertion"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["classification"], "product")

    def test_no_signature_is_a_bare_lifecycle_event_name_when_a_cause_exists(self) -> None:
        for lifecycle in ("blocked:run_failed", "blocked:plan_graph_block_escalated"):
            self.assertNotIn(lifecycle, self.signatures)

    def test_cause_shaped_signatures_stay_secret_free(self) -> None:
        for observation in self.result.observations:
            signature = observation["signature"]
            self.assertNotIn(_ABS_PATH_MARKER, signature)
            self.assertNotRegex(signature, r"\d{4}-\d{2}-\d{2}")
            self.assertLessEqual(
                len(signature), run_forensics.MAX_REASON_SIGNATURE_LENGTH
            )

    def test_every_cause_run_observation_validates_against_the_schema(self) -> None:
        self.assertGreater(len(self.causes), 0)
        for observation in self.causes:
            errors = checker.validate_artifact(observation, observation["signature"])
            self.assertEqual(errors, [], msg=f"schema violations: {errors}")


class WithinRunDedupTests(_CorpusTestCase):
    """DEFECT 2(c): one incident used to echo at every level that reported
    it, inflating the pattern count and hiding the root cause."""

    def setUp(self) -> None:
        super().setUp()
        self.result = mine(self.runs_root, state_root=self.state_root)
        self.causes = self._observations_for(self.result, _CAUSES_RUN_ID)

    def test_coordinator_restating_a_node_cause_mints_no_second_observation(self) -> None:
        """``recovery_decision`` quotes the node's ``blocked_reason``
        verbatim; both resolve to the same cause key, so only the node's own
        (earlier) record survives."""

        required = [obs for obs in self.causes if obs["signature"] == "required_findings_open"]
        self.assertEqual(len(required), 1)

    def test_repeated_incidents_from_the_same_source_are_not_collapsed(self) -> None:
        """Dedup targets cross-level echoes, not genuine repeats: two
        separate command rejections in one run are two incidents."""

        rejections = [
            obs for obs in self.causes if obs["signature"] == "command_rejected:unknown_evidence"
        ]
        self.assertEqual(len(rejections), 2)

    def test_bare_lifecycle_events_are_dropped_when_a_cause_was_observed(self) -> None:
        phases = {obs["signature"] for obs in self.causes}
        self.assertNotIn("blocked:run_failed", phases)

    def test_a_run_with_only_lifecycle_events_is_still_represented(self) -> None:
        """The suppression must never empty a failing run: with nothing else
        to point at, the earliest lifecycle record is kept."""

        lifecycle = self._observations_for(self.result, _LIFECYCLE_RUN_ID)
        self.assertEqual(len(lifecycle), 1)
        self.assertEqual(lifecycle[0]["signature"], "failed:run_failed")

    def test_dedup_materially_reduces_the_pattern_count_for_one_incident(self) -> None:
        """Before the fix, the required-findings incident alone minted a
        ``review_fix_completed`` observation, a ``recovery_decision``
        observation quoting it, and a ``run_failed`` observation -- three
        distinct signatures for one root cause."""

        incident = [
            obs
            for obs in self.causes
            if obs["signature"] in {"required_findings_open"}
            or "required findings" in obs["signature"]
            or obs["signature"].endswith(":run_failed")
        ]
        self.assertEqual(len(incident), 1)


if __name__ == "__main__":
    unittest.main()
