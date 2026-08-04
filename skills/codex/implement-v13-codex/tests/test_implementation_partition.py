from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))

from implementation_partition import derive_partition, ensure_partition, validate_worker_spec  # noqa: E402
from state_io import StateError, atomic_write_json  # noqa: E402


class ImplementationPartitionTests(unittest.TestCase):
    def _linear_plan(self) -> dict[str, object]:
        steps = []
        for index in range(1, 7):
            steps.append({
                "id": f"step-{index}",
                "title": f"bounded slice {index}",
                "effort": 5,
                "dependencies": [] if index == 1 else [f"step-{index - 1}"],
                "write_paths": [f"src/slice_{index}.py", f"tests/test_slice_{index}.py"],
                "targeted_tests": [f"pytest tests/test_slice_{index}.py -q"],
            })
        return {"steps": steps}

    def test_q12_like_linear_plan_is_split_into_three_bounded_groups(self) -> None:
        partition = derive_partition(self._linear_plan())
        self.assertEqual(len(partition["groups"]), 3)
        self.assertEqual([group["step_ids"] for group in partition["groups"]], [
            ["step-1", "step-2"], ["step-3", "step-4"], ["step-5", "step-6"],
        ])
        self.assertEqual(partition["groups"][1]["depends_on"], ["implementation_group_1"])
        self.assertEqual(partition["groups"][2]["depends_on"], ["implementation_group_2"])
        self.assertTrue(all(not group["exceptions"] for group in partition["groups"]))
        schema = json.loads((PACKAGE / "schemas" / "implementation-partition.schema.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(partition)

    def test_worker_spec_must_exactly_match_one_derived_group(self) -> None:
        partition = derive_partition(self._linear_plan())
        group = partition["groups"][0]
        spec = {
            "implementation_group_id": group["group_id"],
            "assigned_step_ids": group["step_ids"],
            "allowed_write_paths": group["write_paths"],
        }
        validate_worker_spec(spec, partition)
        with self.assertRaisesRegex(StateError, "assigned_step_ids mismatch"):
            validate_worker_spec(dict(spec, assigned_step_ids=["step-1"]), partition)

    def test_existing_partition_must_match_current_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            output = root / "partition.json"
            atomic_write_json(plan_path, self._linear_plan())
            first = ensure_partition(plan_path, output)
            self.assertEqual(ensure_partition(plan_path, output), first)
            plan = self._linear_plan()
            plan["steps"][0]["effort"] = 7
            atomic_write_json(plan_path, plan)
            with self.assertRaisesRegex(StateError, "differs from the deterministic"):
                ensure_partition(plan_path, output)


if __name__ == "__main__":
    unittest.main()
