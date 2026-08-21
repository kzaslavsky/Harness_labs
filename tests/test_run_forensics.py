from __future__ import annotations

import importlib.util
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
_FABRICATED_RUN_ID = "run-si02-fabricated-001"
_TAMPERED_RUN_DIR = "run-si02-tampered-001"
_KNOWN_RUN_IDS = (_VALID_RUN_ID, _VALID_RUN_ID_2, _FABRICATED_RUN_ID, _TAMPERED_RUN_DIR)


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
        for run_id in (_VALID_RUN_ID, _VALID_RUN_ID_2):
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


if __name__ == "__main__":
    unittest.main()
