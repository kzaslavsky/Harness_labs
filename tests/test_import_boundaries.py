"""GraphRun layer-boundary contract (GRAPHRUN_RESTRUCTURE_PLAN.md).

The two closure tests were the restructure program's red phase (xfail at
GR-01); GR-02's boundary fixes turned them green and the markers came off. Assertions run on **static AST
import closures** — deferred in-function imports included — never on
``sys.modules``: the package ``__init__`` eagerly re-exports the plangraph
surface, so any runtime probe is red regardless of layering.
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "dev" / "check_import_boundaries.py"

spec = importlib.util.spec_from_file_location("check_import_boundaries", CHECKER)
boundaries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundaries)

PACKAGE_ROOT = REPO / "harness_labs"


class ImportBoundaryTests(unittest.TestCase):
    def test_checker_reports_no_errors(self):
        completed = subprocess.run(
            [sys.executable, str(CHECKER)], capture_output=True, text=True
        )
        self.assertEqual(
            completed.returncode, 0,
            f"boundary checker reported errors:\n{completed.stdout}",
        )

    def test_feature_run_closure_free_of_plangraph(self):
        layers = boundaries.closure_layers("feature_run", PACKAGE_ROOT)
        plangraph_members = sorted(
            stem for stem, layer in layers.items() if layer == "plangraph"
        )
        self.assertEqual(
            plangraph_members, [],
            "feature_run's static import closure reaches plangraph-layer "
            f"modules: {plangraph_members}",
        )

    def test_development_policy_closure_free_of_featurerun(self):
        layers = boundaries.closure_layers("development_policy", PACKAGE_ROOT)
        featurerun_members = sorted(
            stem for stem, layer in layers.items() if layer == "featurerun"
        )
        self.assertEqual(
            featurerun_members, [],
            "development_policy's static import closure (deferred imports "
            f"included) reaches featurerun-layer modules: {featurerun_members}",
        )


if __name__ == "__main__":
    unittest.main()
