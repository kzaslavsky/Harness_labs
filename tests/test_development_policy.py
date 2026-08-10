from __future__ import annotations

import unittest

from harness_labs.coordinator_schema import CoordinatorDispatchSchema
from harness_labs.development_policy import (
    DevelopmentPolicy,
)
from harness_labs.feature_run_policy import (
    standard_feature_run_dispatch_schema,
    standard_feature_run_policy,
)


class DevelopmentPolicyTests(unittest.TestCase):
    def test_policy_round_trip_and_risk_panel_construction(self) -> None:
        policy = standard_feature_run_policy()
        restored = DevelopmentPolicy.from_mapping(policy.as_dict())
        self.assertEqual(restored.sha256(), policy.sha256())
        roles = {
            item.role
            for item in policy.review_assignments(
                ("src/auth/session.ts", "templates/login.html")
            )
        }
        self.assertEqual(
            roles,
            {
                "adversarial-reviewer",
                "correctness-reviewer",
                "security-reviewer",
                "ui-runtime-reviewer",
            },
        )

    def test_portable_schema_carries_policy_and_exit_gates(self) -> None:
        schema = standard_feature_run_dispatch_schema()
        schema.validate_phases(
            (
                "orient",
                "plan",
                "implement",
                "verify",
                "review",
                "integrate",
                "report",
            )
        )
        restored = CoordinatorDispatchSchema.from_mapping(schema.as_dict())
        self.assertEqual(restored.sha256(), schema.sha256())
        planning = restored.segments[0]
        self.assertIn("source-binding-report", planning.exit_artifact_kinds)
        self.assertIsNotNone(planning.development_policy)
        verify = restored.segments[2]
        review = restored.segments[3]
        self.assertEqual(verify.phases, ("verify",))
        self.assertEqual(verify.coordinator_profile, "verification-coordinator")
        self.assertIn("verification-report", verify.exit_artifact_kinds)
        self.assertEqual(review.phases, ("review",))
        self.assertIn("verification-report", review.required_artifact_kinds)
        self.assertIn("review-ledger", review.exit_artifact_kinds)


if __name__ == "__main__":
    unittest.main()
