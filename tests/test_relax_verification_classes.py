"""Finding tests for CB-03: structured-evidence verification classification.

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it imports nothing that does not already exist at the
base commit, and every assertion is a controlled ``assert*``/``self.fail`` so
a base-harness rejection surfaces as a pytest FAILED, never an ERROR.
"""

from __future__ import annotations

import unittest

from harness_labs.feature_run import classify_verification_failure


class RelaxVerificationClassesTests(unittest.TestCase):
    def test_timed_out_flag_classifies_infrastructure_transient(self) -> None:
        result = classify_verification_failure(
            {"timed_out": True, "exit_code": 124, "stdout": "", "stderr": ""}
        )
        self.assertEqual(result["classification"], "infrastructure_transient")
        self.assertNotEqual(result["rule_id"], "conservative-default")

    def test_exit_code_124_without_timed_out_flag_classifies_infrastructure_transient(
        self,
    ) -> None:
        # A caller that only sets the exit code (no timed_out flag) must still
        # be recognized as a timeout from the structured field alone.
        result = classify_verification_failure(
            {"exit_code": 124, "stdout": "", "stderr": ""}
        )
        self.assertEqual(result["classification"], "infrastructure_transient")
        self.assertNotEqual(result["rule_id"], "conservative-default")

    def test_negative_signal_returncode_classifies_infrastructure_transient(
        self,
    ) -> None:
        result = classify_verification_failure(
            {"exit_code": -15, "stdout": "", "stderr": "Terminated"}
        )
        self.assertEqual(result["classification"], "infrastructure_transient")
        self.assertNotEqual(result["rule_id"], "conservative-default")

    def test_structured_classifications_carry_distinct_rule_ids(self) -> None:
        timed_out = classify_verification_failure(
            {"timed_out": True, "exit_code": 124, "stdout": "", "stderr": ""}
        )
        exit_124 = classify_verification_failure(
            {"exit_code": 124, "stdout": "", "stderr": ""}
        )
        signal_terminated = classify_verification_failure(
            {"exit_code": -9, "stdout": "", "stderr": "Killed"}
        )
        rule_ids = {
            timed_out["rule_id"],
            exit_124["rule_id"],
            signal_terminated["rule_id"],
        }
        self.assertEqual(
            len(rule_ids),
            3,
            f"expected three distinct rule ids, got {rule_ids}",
        )
        for outcome in (timed_out, exit_124, signal_terminated):
            self.assertEqual(outcome["classification"], "infrastructure_transient")

    def test_driver_crash_marker_in_pytest_green_output_is_infrastructure_transient(
        self,
    ) -> None:
        stdout = (
            "============================= test session starts ==============================\n"
            "collected 733 items\n"
            "............................................................................\n"
            "============================= 733 passed in 118.42s ==============================\n"
            "FAIL walk driver: browser crashed while walking flow editor route\n"
        )
        result = classify_verification_failure(
            {"exit_code": 1, "stdout": stdout, "stderr": ""}
        )
        self.assertEqual(result["classification"], "infrastructure_transient")

    def test_genuine_assertion_failure_still_classifies_product(self) -> None:
        stdout = (
            "============================= test session starts ==============================\n"
            "collected 733 items\n"
            "FAILED tests/test_widget.py::test_renders - AssertionError: expected 1 got 2\n"
            "========================= 1 failed, 732 passed in 45.10s =========================\n"
        )
        result = classify_verification_failure(
            {"exit_code": 1, "stdout": stdout, "stderr": ""}
        )
        self.assertEqual(result["classification"], "product")
        self.assertEqual(result["rule_id"], "product-assertion")

    def test_generic_browser_text_without_pytest_green_stays_indeterminate(self) -> None:
        # Preexisting conservative behavior for bare browser/selector mentions
        # that carry no structured signal and no pytest-green marker.
        result = classify_verification_failure(
            {"stderr": "browser selector timed out", "stdout": ""}
        )
        self.assertEqual(result["classification"], "indeterminate")
        self.assertEqual(result["rule_id"], "conservative-default")


if __name__ == "__main__":
    unittest.main()
