"""Tests for the join-conflict resolution registry and its join integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from harness_labs.plangraph.plan_graph import (
    PlanGraph,
    PlanGraphError,
    register_plan_graph,
)
from harness_labs.plangraph.plan_graph_join import (
    JoinConflictResolutionStore,
    JoinResolutionError,
    describe_join_conflict,
)


def git(repository: Path, *arguments: str, env: dict | None = None) -> str:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, text=True,
        capture_output=True, check=False, env=merged_env,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def build_conflicting_repository(root: Path) -> tuple[Path, str, str, str]:
    """Return (repository, base, side_a, side_b) with a real content conflict.

    ``shared.txt`` conflicts between the siblings; ``clean_a.txt`` and
    ``clean_b.txt`` merge automatically.
    """
    repository = root / "repo"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.email", "test@example.com")
    git(repository, "config", "user.name", "Test")
    (repository / "shared.txt").write_text("alpha\ncommon\nomega\n")
    (repository / "keep.txt").write_text("untouched\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")

    (repository / "shared.txt").write_text("alpha-from-a\ncommon\nomega\n")
    (repository / "clean_a.txt").write_text("a only\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "side a")
    side_a = git(repository, "rev-parse", "HEAD")

    git(repository, "checkout", "--quiet", base)
    (repository / "shared.txt").write_text("alpha-from-b\ncommon\nomega\n")
    (repository / "clean_b.txt").write_text("b only\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "side b")
    side_b = git(repository, "rev-parse", "HEAD")
    return repository, base, side_a, side_b


def replace_blobs_in_tree(
    repository: Path, tree: str, replacements: dict[str, str]
) -> str:
    """Build a new tree from ``tree`` with the given path contents replaced."""
    with tempfile.NamedTemporaryFile(prefix="join-index-") as index:
        env = {"GIT_INDEX_FILE": index.name}
        git(repository, "read-tree", tree, env=env)
        for path, content in replacements.items():
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repository, input=content, text=True,
                capture_output=True, check=True,
            ).stdout.strip()
            git(
                repository, "update-index", "--add",
                "--cacheinfo", f"100644,{blob},{path}", env=env,
            )
        return git(repository, "write-tree", env=env)


def minimal_plan_graph(repository: Path, run_root: Path) -> PlanGraph:
    """A structurally minimal PlanGraph exposing the real ``_join_candidates``.

    Full construction requires an approved registration and launcher; the
    join step only touches the attributes set here, so this keeps the test
    on the production code path without replicating the whole campaign
    bootstrap.
    """
    graph = PlanGraph.__new__(PlanGraph)
    graph.repository = repository.resolve()
    graph.run_root = run_root.resolve()
    graph.graph_run_id = "plan-graph-join-test"
    graph.registration = SimpleNamespace(plan_lineage_id="join-test-lineage")
    graph.join_resolutions = JoinConflictResolutionStore(
        run_root, "join-test-lineage", repository
    )
    return graph


class DescribeJoinConflictTest(unittest.TestCase):
    def test_describes_real_conflict_with_full_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, base, side_a, side_b = build_conflicting_repository(Path(tmp))
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            self.assertEqual(description["parents"], [side_a, side_b])
            self.assertEqual(description["conflicted_paths"], ["shared.txt"])
            self.assertIn("<<<<<<<", description["conflicted_files"]["shared.txt"])
            self.assertIn("alpha-from-a", description["conflicted_files"]["shared.txt"])
            self.assertIn("alpha-from-b", description["conflicted_files"]["shared.txt"])
            self.assertIn("CONFLICT", description["merge_tree_output"])
            self.assertEqual(len(description["resolution_key"]), 64)
            base_tree = git(repository, "rev-parse", f"{base}^{{tree}}")
            self.assertEqual(description["merge_base_trees"], [base_tree])

    def test_refuses_clean_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, base, side_a, _ = build_conflicting_repository(Path(tmp))
            with self.assertRaisesRegex(JoinResolutionError, "merge\\s+cleanly"):
                describe_join_conflict(repository, "wp", base, side_a)

    def test_key_is_stable_across_commit_identity_changes(self) -> None:
        # The same trees reached through re-created commits (fresh committer
        # metadata) must key to the same resolution: synthetic intermediate
        # join commits are rebuilt per attempt with new timestamps.
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            first = describe_join_conflict(repository, "wp", side_a, side_b)
            rebuilt = git(
                repository, "commit-tree", f"{side_a}^{{tree}}",
                "-p", f"{side_a}^", "-m", "rebuilt with different metadata",
                env={
                    "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z",
                },
            )
            self.assertNotEqual(rebuilt, side_a)
            second = describe_join_conflict(repository, "wp", rebuilt, side_b)
            self.assertEqual(first["resolution_key"], second["resolution_key"])


class JoinConflictResolutionStoreTest(unittest.TestCase):
    def test_register_and_lookup_verified_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            run_root = Path(tmp) / "runs"
            store = JoinConflictResolutionStore(run_root, "lineage", repository)
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            resolved_tree = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {"shared.txt": "alpha-resolved\ncommon\nomega\n"},
            )
            record = store.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=resolved_tree, reason="hand-merged alpha token",
            )
            self.assertEqual(record["sequence"], 1)
            self.assertEqual(record["resolved_tree"], resolved_tree)
            self.assertEqual(record["conflicted_paths"], ["shared.txt"])
            found = store.lookup(label="wp", parent_a=side_a, parent_b=side_b)
            self.assertEqual(found["resolved_tree"], resolved_tree)
            # The resolved tree is anchored against gc.
            anchored = git(
                repository, "rev-parse",
                f"refs/plan-graph-join/lineage/{record['resolution_key'][:16]}",
            )
            self.assertEqual(anchored, resolved_tree)
            # Idempotent re-registration returns the existing record.
            again = store.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=resolved_tree, reason="same again",
            )
            self.assertEqual(again["sequence"], 1)

    def test_lookup_misses_for_other_label_or_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, base, side_a, side_b = build_conflicting_repository(Path(tmp))
            run_root = Path(tmp) / "runs"
            store = JoinConflictResolutionStore(run_root, "lineage", repository)
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            resolved_tree = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {"shared.txt": "alpha-resolved\ncommon\nomega\n"},
            )
            store.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=resolved_tree, reason="hand-merged",
            )
            self.assertIsNone(
                store.lookup(label="other", parent_a=side_a, parent_b=side_b)
            )
            self.assertIsNone(
                store.lookup(label="wp", parent_a=base, parent_b=side_b)
            )

    def test_register_rejects_pair_without_real_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, base, side_a, _ = build_conflicting_repository(Path(tmp))
            store = JoinConflictResolutionStore(
                Path(tmp) / "runs", "lineage", repository
            )
            tree = git(repository, "rev-parse", f"{side_a}^{{tree}}")
            with self.assertRaisesRegex(JoinResolutionError, "merge\\s+cleanly"):
                store.register(
                    label="wp", parent_a=base, parent_b=side_a,
                    resolved_tree=tree, reason="not a conflict",
                )

    def test_register_rejects_tree_touching_unconflicted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            store = JoinConflictResolutionStore(
                Path(tmp) / "runs", "lineage", repository
            )
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            smuggled = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {
                    "shared.txt": "alpha-resolved\ncommon\nomega\n",
                    "keep.txt": "smuggled payload\n",
                },
            )
            with self.assertRaisesRegex(JoinResolutionError, "outside the observed conflict"):
                store.register(
                    label="wp", parent_a=side_a, parent_b=side_b,
                    resolved_tree=smuggled, reason="smuggle attempt",
                )

    def test_register_rejects_unrelated_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, base, side_a, side_b = build_conflicting_repository(Path(tmp))
            store = JoinConflictResolutionStore(
                Path(tmp) / "runs", "lineage", repository
            )
            unrelated = git(repository, "rev-parse", f"{base}^{{tree}}")
            with self.assertRaises(JoinResolutionError):
                store.register(
                    label="wp", parent_a=side_a, parent_b=side_b,
                    resolved_tree=unrelated, reason="hand-wave",
                )

    def test_register_rejects_automerge_tree_with_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            store = JoinConflictResolutionStore(
                Path(tmp) / "runs", "lineage", repository
            )
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            with self.assertRaisesRegex(JoinResolutionError, "not a resolution"):
                store.register(
                    label="wp", parent_a=side_a, parent_b=side_b,
                    resolved_tree=description["automerge_tree"],
                    reason="markers left in",
                )

    def test_register_rejects_missing_tree_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            store = JoinConflictResolutionStore(
                Path(tmp) / "runs", "lineage", repository
            )
            with self.assertRaisesRegex(JoinResolutionError, "not a tree object"):
                store.register(
                    label="wp", parent_a=side_a, parent_b=side_b,
                    resolved_tree="0" * 40, reason="bogus",
                )

    def test_supersede_required_for_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            store = JoinConflictResolutionStore(
                Path(tmp) / "runs", "lineage", repository
            )
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            first = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {"shared.txt": "alpha-first\ncommon\nomega\n"},
            )
            second = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {"shared.txt": "alpha-second\ncommon\nomega\n"},
            )
            store.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=first, reason="first resolution",
            )
            with self.assertRaisesRegex(JoinResolutionError, "supersede"):
                store.register(
                    label="wp", parent_a=side_a, parent_b=side_b,
                    resolved_tree=second, reason="replacement",
                )
            record = store.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=second, reason="replacement", supersede=True,
            )
            self.assertEqual(record["sequence"], 2)
            self.assertEqual(record["supersedes"], 1)
            found = store.lookup(label="wp", parent_a=side_a, parent_b=side_b)
            self.assertEqual(found["resolved_tree"], second)
            self.assertEqual(len(store.records()), 2)


class JoinCandidatesResolutionTest(unittest.TestCase):
    def test_unresolved_conflict_raises_with_full_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            run_root = Path(tmp) / "runs"
            run_root.mkdir()
            graph = minimal_plan_graph(repository, run_root)
            with self.assertRaises(PlanGraphError) as caught:
                graph._join_candidates("wp", [side_a, side_b])
            message = str(caught.exception)
            self.assertIn(side_a, message)
            self.assertIn(side_b, message)
            self.assertIn("shared.txt", message)
            self.assertIn("plan defect", message)
            artifact_path = Path(message.rsplit("Full diagnostics: ", 1)[1])
            self.assertTrue(artifact_path.exists())
            artifact = json.loads(artifact_path.read_text())
            self.assertEqual(artifact["label"], "wp")
            self.assertEqual(artifact["parents"], [side_a, side_b])
            self.assertEqual(artifact["conflicted_paths"], ["shared.txt"])
            self.assertIn("<<<<<<<", artifact["conflicted_files"]["shared.txt"])
            self.assertIn("CONFLICT", artifact["merge_tree_output"])
            self.assertIn("register", artifact["resolution_registration"]["argv"])

    def test_registered_resolution_lets_join_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            run_root = Path(tmp) / "runs"
            run_root.mkdir()
            graph = minimal_plan_graph(repository, run_root)
            description = describe_join_conflict(repository, "wp", side_a, side_b)
            resolved_tree = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {"shared.txt": "alpha-resolved\ncommon\nomega\n"},
            )
            graph.join_resolutions.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=resolved_tree, reason="hand-merged alpha token",
            )
            merged = graph._join_candidates("wp", [side_a, side_b])
            self.assertEqual(
                git(repository, "rev-parse", f"{merged}^{{tree}}"), resolved_tree
            )
            parents = git(
                repository, "rev-list", "--parents", "-n", "1", merged
            ).split()[1:]
            self.assertEqual(parents, [side_a, side_b])
            subject = git(repository, "log", "-1", "--format=%s", merged)
            self.assertTrue(subject.startswith("PlanGraph join wp ("))
            self.assertIn("conflict resolved", subject)
            # The auto-merged siblings' clean files survive untouched.
            self.assertEqual(
                git(repository, "show", f"{merged}:clean_a.txt"), "a only"
            )
            self.assertEqual(
                git(repository, "show", f"{merged}:clean_b.txt"), "b only"
            )

    def test_resolution_for_other_label_does_not_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository, _, side_a, side_b = build_conflicting_repository(Path(tmp))
            run_root = Path(tmp) / "runs"
            run_root.mkdir()
            graph = minimal_plan_graph(repository, run_root)
            description = describe_join_conflict(repository, "other", side_a, side_b)
            resolved_tree = replace_blobs_in_tree(
                repository, description["automerge_tree"],
                {"shared.txt": "alpha-resolved\ncommon\nomega\n"},
            )
            graph.join_resolutions.register(
                label="other", parent_a=side_a, parent_b=side_b,
                resolved_tree=resolved_tree, reason="different join",
            )
            with self.assertRaisesRegex(PlanGraphError, "no verified resolution"):
                graph._join_candidates("wp", [side_a, side_b])


class JoinResolutionStoreWiringTest(unittest.TestCase):
    """The ``__init__`` wiring for ``self.join_resolutions``.

    Every other test in this file reaches ``_join_candidates`` through
    ``minimal_plan_graph``, which builds the graph with ``PlanGraph.__new__``
    and constructs the store by hand.  That keeps those tests on the
    production join path but leaves the three constructor arguments at
    ``PlanGraph.__init__`` uncovered, so this builds a real registered graph
    and pins them.
    """

    def _registered_graph(self, root: Path) -> PlanGraph:
        repository = root / "repository"
        repository.mkdir()
        git(repository, "init")
        git(repository, "config", "user.email", "tests@example.com")
        git(repository, "config", "user.name", "Tests")
        plan = repository / "docs" / "approved-plan.md"
        plan.parent.mkdir()
        plan.write_text("Approved PlanGraph plan\n", encoding="utf-8")
        git(repository, "add", "docs/approved-plan.md")
        git(repository, "commit", "-m", "approved plan")
        base_commit = git(repository, "rev-parse", "HEAD")
        registration = register_plan_graph(
            repository=repository,
            logical_graph_id="join-wiring-graph",
            decomposition={
                "plan": "docs/approved-plan.md",
                "base_commit": base_commit,
                "runs": [
                    {
                        "id": "a",
                        "objective": "Build A",
                        "plan_sections": ["1"],
                        "criteria": ["AC-1"],
                        "depends_on": [],
                        "verification_argv": ["python3", "-m", "unittest"],
                    },
                ],
                "plan_sections": {"1": "Build A. AC-1: A works."},
                "acceptance_criteria": {"AC-1": "A works."},
                "functionality_tests": [],
            },
        )
        return PlanGraph(
            repository,
            registration,
            lambda request: None,
            run_root=root / "runs",
            graph_run_id="join-wiring-attempt",
        )

    def test_init_binds_store_to_run_root_lineage_and_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = self._registered_graph(root)
            store = graph.join_resolutions
            self.assertIsInstance(store, JoinConflictResolutionStore)
            lineage = graph.registration.plan_lineage_id
            self.assertEqual(store.lineage_id, lineage)
            self.assertEqual(
                store.path,
                (root / "runs").resolve()
                / ".plan-graph-join-resolutions"
                / f"{lineage}.jsonl",
            )
            self.assertEqual(store.repository, (root / "repository").resolve())

    def test_store_from_init_is_the_one_the_join_consults(self) -> None:
        """A registration made through the constructed graph's own store is
        the one ``_join_candidates`` finds -- i.e. the wiring is live, not
        merely well-shaped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = self._registered_graph(root)
            conflicting_root = root / "conflicting"
            conflicting_root.mkdir()
            conflicting, _, side_a, side_b = build_conflicting_repository(
                conflicting_root
            )
            # Point the graph at the conflicting repository while keeping the
            # store its ``__init__`` built, so the join reads through it.
            graph.repository = conflicting.resolve()
            graph.join_resolutions.repository = conflicting.resolve()

            with self.assertRaises(PlanGraphError):
                graph._join_candidates("wp", [side_a, side_b])

            description = describe_join_conflict(conflicting, "wp", side_a, side_b)
            resolved_tree = replace_blobs_in_tree(
                conflicting, description["automerge_tree"],
                {"shared.txt": "alpha-resolved\ncommon\nomega\n"},
            )
            record = graph.join_resolutions.register(
                label="wp", parent_a=side_a, parent_b=side_b,
                resolved_tree=resolved_tree, reason="wiring test resolution",
            )
            self.assertTrue(graph.join_resolutions.path.exists())

            merged = graph._join_candidates("wp", [side_a, side_b])
            self.assertEqual(
                git(conflicting, "rev-parse", f"{merged}^{{tree}}"),
                record["resolved_tree"],
            )


def _resolve_marker_blocks(content: str, choose) -> str:
    """Resolve conflict-marker blocks with a per-block strategy callback.

    ``choose(index, ours_lines, theirs_lines)`` returns the resolved lines
    for the ``index``-th conflict block in file order.
    """
    lines = content.split("\n")
    output: list[str] = []
    index = 0
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor]
        if line.startswith("<<<<<<< "):
            ours: list[str] = []
            theirs: list[str] = []
            cursor += 1
            while not lines[cursor].startswith("======="):
                ours.append(lines[cursor])
                cursor += 1
            cursor += 1
            while not lines[cursor].startswith(">>>>>>> "):
                theirs.append(lines[cursor])
                cursor += 1
            output.extend(choose(index, ours, theirs))
            index += 1
        else:
            output.append(line)
        cursor += 1
    return "\n".join(output)


RETINOLOGY_REPO = Path(
    os.environ.get(
        "RETINOLOGY_REPO", "/Users/kirillzaslavsky/claudeprojects/Retinology"
    )
)
WP25_PARENT_A = "958850700100f120b7ab0e419b3287a6613ccc90"
WP25_PARENT_B = "774cbe751b0875635bc327eb6cacb26706a310ca"


def _retinology_fixture_available() -> bool:
    if not (RETINOLOGY_REPO / ".git").exists() and not (
        RETINOLOGY_REPO / "HEAD"
    ).exists():
        return False
    for commit in (WP25_PARENT_A, WP25_PARENT_B):
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=RETINOLOGY_REPO, capture_output=True, check=False,
        )
        if probe.returncode:
            return False
    return True


REQUIRE_RETINOLOGY_FIXTURE = bool(
    os.environ.get("HARNESS_LABS_REQUIRE_RETINOLOGY_FIXTURE")
)
_RETINOLOGY_FIXTURE_AVAILABLE = _retinology_fixture_available()

# This class is the only coverage anywhere of a join resolution against real
# conflicting content; the rest of the file is synthetic.  Skipping it is
# legitimate off the machine that holds the fixture, but doing so silently
# would let a run report green while the strongest test in the file never
# executed -- so announce it, and let CI demand it with
# HARNESS_LABS_REQUIRE_RETINOLOGY_FIXTURE=1.
_RETINOLOGY_FIXTURE_MISSING = (
    f"Retinology fixture repository not available at {RETINOLOGY_REPO} "
    f"(set RETINOLOGY_REPO to a clone containing {WP25_PARENT_A[:12]} and "
    f"{WP25_PARENT_B[:12]}); the real WP-25 join-conflict regression test is "
    "NOT running."
)
_RETINOLOGY_SKIP_REASON = (
    f"{_RETINOLOGY_FIXTURE_MISSING} Set "
    "HARNESS_LABS_REQUIRE_RETINOLOGY_FIXTURE=1 to make this a failure "
    "instead of a skip."
)
if not _RETINOLOGY_FIXTURE_AVAILABLE and not REQUIRE_RETINOLOGY_FIXTURE:
    print(f"WARNING: {_RETINOLOGY_SKIP_REASON}", file=sys.stderr)


@unittest.skipUnless(
    _RETINOLOGY_FIXTURE_AVAILABLE or REQUIRE_RETINOLOGY_FIXTURE,
    _RETINOLOGY_SKIP_REASON,
)
class RetinologyWp25EndToEndTest(unittest.TestCase):
    """The real WP-25 join conflict (sealed WP-21 x WP-22), end to end.

    The fixture repository is mirror-cloned into a temporary directory so the
    live clone is never written to; the resolution is built exactly as the
    operator diagnosed it: pick the WP-22 color token in the CSS, keep the
    WP-21 import-list superset in the JS, and union the two coverage
    obligations in the parity oracle.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not _RETINOLOGY_FIXTURE_AVAILABLE:
            # Only reachable with HARNESS_LABS_REQUIRE_RETINOLOGY_FIXTURE set,
            # which asks for a failure rather than a skip.
            raise AssertionError(_RETINOLOGY_FIXTURE_MISSING)

    def test_wp25_conflict_resolves_through_registered_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp) / "retinology.git"
            subprocess.run(
                ["git", "clone", "--quiet", "--mirror", str(RETINOLOGY_REPO), str(mirror)],
                capture_output=True, check=True,
            )
            run_root = Path(tmp) / "runs"
            run_root.mkdir()
            graph = minimal_plan_graph(mirror, run_root)
            graph.registration = SimpleNamespace(plan_lineage_id="retinology-wp25")
            graph.join_resolutions = JoinConflictResolutionStore(
                run_root, "retinology-wp25", mirror
            )

            # 1. The unresolved conflict surfaces with full diagnostics.
            with self.assertRaises(PlanGraphError) as caught:
                graph._join_candidates("WP-25", [WP25_PARENT_A, WP25_PARENT_B])
            message = str(caught.exception)
            for path in (
                "retinology/web/static/css/flow_editor.css",
                "retinology/web/static/js/flow/canvas.js",
                "tests/test_flow_parity_oracle.py",
            ):
                self.assertIn(path, message)
            self.assertNotIn("tests/test_l2_flow_editor.py", message)

            # 2. Build the operator's resolution from the described conflict.
            description = describe_join_conflict(
                mirror, "WP-25", WP25_PARENT_A, WP25_PARENT_B
            )
            self.assertEqual(
                sorted(description["conflicted_paths"]),
                [
                    "retinology/web/static/css/flow_editor.css",
                    "retinology/web/static/js/flow/canvas.js",
                    "tests/test_flow_parity_oracle.py",
                ],
            )
            files = description["conflicted_files"]
            css = _resolve_marker_blocks(
                files["retinology/web/static/css/flow_editor.css"],
                lambda index, ours, theirs: theirs,  # WP-22 color token pick
            )
            javascript = _resolve_marker_blocks(
                files["retinology/web/static/js/flow/canvas.js"],
                lambda index, ours, theirs: ours,  # WP-21 list is the superset
            )

            def resolve_oracle(index: int, ours: list[str], theirs: list[str]):
                if index == 0:
                    return ours + theirs  # keep WP-21 fixtures and WP-22 gate
                # Second hunk: WP-22 logic, with WP-21's import selectors
                # folded back into the coverage set.
                return [
                    line.replace(
                        'covered = {s for entry in contract.values() for s in entry["selectors"]}',
                        'covered = {s for entry in contract.values() for s in entry["selectors"]} | _WP21_IMPORT_PAINT_SELECTORS',
                    )
                    for line in theirs
                ]

            oracle = _resolve_marker_blocks(
                files["tests/test_flow_parity_oracle.py"], resolve_oracle
            )
            self.assertIn("_WP21_IMPORT_PAINT_SELECTORS", oracle)
            self.assertIn("_WP22_VISUAL_PAINT_SELECTORS", oracle)
            resolved_tree = replace_blobs_in_tree(
                mirror, description["automerge_tree"],
                {
                    "retinology/web/static/css/flow_editor.css": css,
                    "retinology/web/static/js/flow/canvas.js": javascript,
                    "tests/test_flow_parity_oracle.py": oracle,
                },
            )

            # 3. Register it and prove the mechanical join now succeeds.
            record = graph.join_resolutions.register(
                label="WP-25", parent_a=WP25_PARENT_A, parent_b=WP25_PARENT_B,
                resolved_tree=resolved_tree,
                reason=(
                    "WP-21 x WP-22 sibling conflict, hand-diagnosed: WP-22 "
                    "chip color token, WP-21 import superset, union of parity "
                    "oracle coverage obligations"
                ),
            )
            merged = graph._join_candidates(
                "WP-25", [WP25_PARENT_A, WP25_PARENT_B]
            )
            self.assertEqual(
                git(mirror, "rev-parse", f"{merged}^{{tree}}"),
                record["resolved_tree"],
            )
            parents = git(
                mirror, "rev-list", "--parents", "-n", "1", merged
            ).split()[1:]
            self.assertEqual(parents, [WP25_PARENT_A, WP25_PARENT_B])
            # The resolved files carry no markers and the auto-merged file
            # (tests/test_l2_flow_editor.py) is the mechanical union.
            for path in description["conflicted_paths"]:
                blob = git(mirror, "show", f"{merged}:{path}")
                self.assertNotIn("<<<<<<<", blob)
                self.assertNotIn(">>>>>>>", blob)


if __name__ == "__main__":
    unittest.main()
