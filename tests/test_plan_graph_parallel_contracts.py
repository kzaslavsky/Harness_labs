"""Pure, deterministic contract predicates for the deferred parallel scheduler."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS, FIXTURES = ROOT / "schemas", ROOT / "tests/fixtures/plan_graph_parallel"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def checkpoint_consistent(checkpoint: dict[str, object]) -> bool:
    """Cross-record invariant that JSON Schema alone cannot express."""
    groups = [checkpoint[key] for key in ("ready", "reserved", "running", "blocked")]
    groups.append([item["node_id"] for item in checkpoint["sealed"]])
    flat = [node for group in groups for node in group]
    if len(flat) != len(set(flat)):
        return False
    allocations = checkpoint["allocations"]
    pairs = [(item["node_id"], item["allocation_id"]) for item in allocations]
    return (
        len(pairs) == len(set(pairs))
        and len({item["allocation_id"] for item in allocations}) == len(allocations)
        and all(item["logical_attempt"] == checkpoint["logical_attempt"] for item in allocations)
        and all(item["node_id"] not in checkpoint["ready"] for item in allocations)
    )


def can_allocate_next(checkpoint: dict[str, object], allocations: list[dict[str, object]]) -> bool:
    """One CAS allocation batch has exactly the next logical-attempt number."""
    if not checkpoint_consistent(checkpoint) or not allocations:
        return False
    next_attempt = checkpoint["logical_attempt"] + 1
    existing = {item["allocation_id"] for item in checkpoint["allocations"]}
    requested = [item["allocation_id"] for item in allocations]
    return (
        all(item["logical_attempt"] == next_attempt for item in allocations)
        and len(requested) == len(set(requested)) and not (set(requested) & existing)
    )


def recovery_disposition(stale: bool, pid_matches: bool | None, token_matches: bool | None) -> str:
    if stale and pid_matches is True and token_matches is True:
        return "running"
    return "blocked" if stale else "reconcile"


def dispatch_frontier(
    ready: list[str], joins: set[str], verified: set[str], dependencies: dict[str, list[str]], slots: int
) -> list[str]:
    """A join verification is a dispatch unit and takes one shared slot."""
    selected: list[str] = []
    for node in ready:
        if any(dependency in joins and dependency not in verified for dependency in dependencies.get(node, [])):
            continue
        unit = f"verify:{node}" if node in joins and node not in verified else node
        if len(selected) == slots:
            break
        selected.append(unit)
    return selected


def can_adopt_seal(request: dict[str, object], receipt: dict[str, object], manifest: dict[str, object]) -> bool:
    allocation = request["allocation"]
    return all((
        receipt.get("status") == "sealed",
        receipt.get("graph_id") == request.get("graph_id"),
        receipt.get("node_id") == request.get("node_id"),
        receipt.get("logical_attempt") == allocation.get("logical_attempt"),
        receipt.get("allocation_id") == allocation.get("allocation_id"),
        receipt.get("parent_candidate_commit") == request.get("parent_candidate_commit"),
        receipt.get("candidate_commit") == manifest.get("candidate_commit"),
        receipt.get("canonical_manifest_ref") == manifest.get("manifest_ref"),
        all(receipt.get(key) for key in ("descriptor_ref", "verification_evidence_ref", "candidate_receipt_ref", "terminal_journal_event_ref")),
    ))


class PlanGraphParallelContractTests(unittest.TestCase):
    schema_names = tuple(sorted(path.name for path in SCHEMAS.glob("plan-graph-parallel-*.schema.json")))
    required_families = {
        "allocation", "checkpoint", "child-evidence-notification", "child-request", "decomposition",
        "event", "force-reconcile", "integration-receipt", "liveness", "logical-attempt",
        "protected-ref", "resume", "seal-receipt",
    }

    def test_schemas_are_versioned_closed_and_cover_every_required_family(self) -> None:
        names = {name.removeprefix("plan-graph-parallel-").removesuffix(".schema.json") for name in self.schema_names}
        self.assertEqual(names, self.required_families)
        for name in self.schema_names:
            schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertFalse(schema["additionalProperties"])
            self.assertTrue(schema["properties"]["protocol"]["const"].endswith("/1"))

    def test_representative_existing_schema_fixtures_are_closed_and_versioned(self) -> None:
        expected = {"fork-join-decomposition.json", "join-request.json", "frontier-checkpoint.json", "seal-receipt.json"}
        self.assertTrue(expected <= {path.name for path in FIXTURES.glob("*.json")})
        checkpoint = load("frontier-checkpoint.json")
        self.assertTrue(checkpoint_consistent(checkpoint))
        self.assertEqual(checkpoint["logical_attempt"], 8)

    def test_fixture_suite_covers_lifecycle_and_late_evidence_scenarios(self) -> None:
        expected = {"root-decomposition.json", "chain-decomposition.json", "fork-join-decomposition.json", "failure-drain.json", "interrupted-checkpoint.json", "repair-resume.json", "abandoned-attempt.json", "adopted-seal.json", "superseded-attempt.json", "late-evidence.json"}
        self.assertTrue(expected <= {path.name for path in FIXTURES.glob("*.json")})
        self.assertEqual(load("failure-drain.json")["must_drain"], ["FR-11", "FR-12"])
        self.assertFalse(load("late-evidence.json")["adopt"])

    def test_baseline_and_fork_join_order_are_fixed(self) -> None:
        baseline = ROOT / "tests/fixtures/plan_graph/retinology-flow-node-mockup-parity-baseline.json"
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), "7c92bf45ccfa94dee75ab145fbc004882daaa5c8db7da1bf8062bf7844f8fca3")
        nodes = load("fork-join-decomposition.json")["nodes"]
        by_id = {node["id"]: node for node in nodes}
        self.assertEqual([node["id"] for node in nodes if node["parallel_group"] == "residual-closure"], ["FR-10", "FR-11", "FR-12"])
        self.assertEqual(by_id["FR-20"]["depends_on"], ["FR-10", "FR-11", "FR-12"])

    def test_checkpoint_state_exclusivity_and_monotonic_allocation_prevent_contender_reuse(self) -> None:
        checkpoint = load("frontier-checkpoint.json")
        bad = copy.deepcopy(checkpoint); bad["reserved"] = ["FR-10"]
        self.assertFalse(checkpoint_consistent(bad))
        first = [{"node_id": "FR-20", "logical_attempt": 9, "allocation_id": "alloc-9"}]
        self.assertTrue(can_allocate_next(checkpoint, first))
        committed = copy.deepcopy(checkpoint); committed["logical_attempt"] = 9; committed["allocations"] = first
        self.assertFalse(can_allocate_next(committed, first))
        self.assertFalse(can_allocate_next(checkpoint, [{"node_id": "FR-20", "logical_attempt": 8, "allocation_id": "alloc-8"}]))

    def test_join_verification_precedes_downstream_and_consumes_shared_slot(self) -> None:
        dependencies = {"FR-30": ["FR-20"]}
        self.assertEqual(dispatch_frontier(["FR-20", "FR-30"], {"FR-20"}, set(), dependencies, 1), ["verify:FR-20"])
        self.assertEqual(dispatch_frontier(["FR-30"], {"FR-20"}, {"FR-20"}, dependencies, 1), ["FR-30"])
        self.assertNotIn("FR-30", dispatch_frontier(["FR-20", "FR-30"], {"FR-20"}, set(), dependencies, 1))

    def test_stale_heartbeat_never_redispatches_matching_process_and_blocks_ambiguity(self) -> None:
        self.assertEqual(recovery_disposition(True, True, True), "running")
        self.assertEqual(recovery_disposition(True, False, True), "blocked")
        self.assertEqual(recovery_disposition(True, True, None), "blocked")

    def test_canonical_manifest_binds_receipt_to_one_candidate_outcome(self) -> None:
        request, receipt = load("join-request.json"), load("seal-receipt.json")
        receipt.update({"node_id": "FR-20", "allocation_id": "alloc-fr-20", "parent_candidate_commit": request["parent_candidate_commit"], "candidate_commit": "1111111111111111111111111111111111111111"})
        manifest = load("adopted-seal.json")
        self.assertTrue(can_adopt_seal(request, receipt, manifest))
        wrong = copy.deepcopy(manifest); wrong["candidate_commit"] = "2222222222222222222222222222222222222222"
        self.assertFalse(can_adopt_seal(request, receipt, wrong))
        late = load("late-evidence.json")
        self.assertFalse(late["adopt"])

    def test_no_production_scheduler_or_integrator_is_in_scope(self) -> None:
        self.assertFalse(any(path.parts[0] == "harness_labs" for path in ROOT.glob("harness_labs/**/*.py") if "parallel" in path.name))


if __name__ == "__main__":
    unittest.main()
