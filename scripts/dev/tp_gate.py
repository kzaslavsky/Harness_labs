#!/usr/bin/env python3
"""Deterministic suite gate for the test-pruner PlanGraph decomposition.

Runs the full unittest suite minus test modules whose host dependencies are
absent (currently: modules that shell out to pytest), printing every
exclusion so the gate stays honest about its coverage. Exit status is 0 only
when every executed test passes. This exists because the graph-level
functionality gate must be green at the base commit, and plain
``unittest discover`` is red on hosts without pytest.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# test module -> host dependency that must be importable for the module to run
CONDITIONAL_MODULES = {
    "tests.test_relax_gate_timeout_classification": "pytest",
}


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-dir", default="tests")
    parser.add_argument("--verbosity", type=int, default=1)
    args = parser.parse_args()

    excluded = {
        module: dependency
        for module, dependency in CONDITIONAL_MODULES.items()
        if importlib.util.find_spec(dependency) is None
    }
    for module, dependency in sorted(excluded.items()):
        print(f"tp_gate: excluding {module} (host lacks {dependency})")

    discovered = unittest.TestLoader().discover(args.start_dir)
    selected = unittest.TestSuite(
        test
        for test in _flatten(discovered)
        if not any(test.id().startswith(module.split(".", 1)[1] + ".")
                   or test.id().startswith(module + ".")
                   for module in excluded)
    )
    result = unittest.TextTestRunner(verbosity=args.verbosity).run(selected)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
