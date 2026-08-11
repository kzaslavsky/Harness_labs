"""Git worktree transaction and integration tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_labs.git_transaction import (
    GitTransactionError,
    GitWorktreeTransaction,
)


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def repository(root: Path) -> Path:
    repo = root / "base"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Harness Tests")
    git(repo, "config", "user.email", "harness@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--no-gpg-sign", "-m", "base")
    return repo


class GitWorktreeTransactionTests(unittest.TestCase):
    def test_create_commit_and_optional_no_merge_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = repository(root)
            worktree = root / "feature"
            transaction = GitWorktreeTransaction.create(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/demo",
                worktree_path=worktree,
            )
            creation = transaction.creation_receipt()
            (worktree / "src").mkdir()
            (worktree / "src" / "feature.txt").write_text(
                "candidate\n", encoding="utf-8"
            )

            commit = transaction.commit_candidate(
                allowed_paths=("src",),
                message="Add feature",
            )
            integration = transaction.integrate(merge=False)

            self.assertEqual(
                creation["verified_head"],
                transaction.base_commit,
            )
            self.assertEqual(commit["changed_paths"], ["src/feature.txt"])
            self.assertEqual(integration["status"], "ready_not_merged")
            self.assertEqual(git(base, "rev-parse", "HEAD"), transaction.base_commit)

    def test_commit_rejects_changes_outside_declared_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = repository(root)
            transaction = GitWorktreeTransaction.create(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/scope",
                worktree_path=root / "feature",
            )
            (transaction.worktree_path / "allowed.txt").write_text(
                "allowed\n", encoding="utf-8"
            )
            (transaction.worktree_path / "forbidden.txt").write_text(
                "forbidden\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                GitTransactionError, "outside scope.*forbidden.txt"
            ):
                transaction.commit_candidate(
                    allowed_paths=("allowed.txt",),
                    message="Scoped change",
                )

    def test_create_can_pin_a_lane_to_an_immutable_parent_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = repository(root)
            parent = git(base, "rev-parse", "HEAD")
            (base / "later.txt").write_text("later\n", encoding="utf-8")
            git(base, "add", "later.txt")
            git(base, "commit", "--no-gpg-sign", "-m", "Advance main")

            transaction = GitWorktreeTransaction.create(
                base_repository=base,
                base_branch="main",
                feature_branch="lane/immutable-parent",
                worktree_path=root / "lane",
                base_commit=parent,
            )

            self.assertEqual(transaction.base_commit, parent)
            self.assertEqual(git(transaction.worktree_path, "rev-parse", "HEAD"), parent)
            self.assertFalse((transaction.worktree_path / "later.txt").exists())

    def test_merge_rejects_stale_base_and_success_reads_back_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = repository(root)
            transaction = GitWorktreeTransaction.create(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/merge",
                worktree_path=root / "feature",
            )
            (transaction.worktree_path / "feature.txt").write_text(
                "feature\n", encoding="utf-8"
            )
            transaction.commit_candidate(
                allowed_paths=("feature.txt",),
                message="Feature",
            )
            receipt = transaction.integrate(merge=True)

            self.assertEqual(receipt["status"], "merged")
            self.assertEqual(receipt["merge_commit"], git(base, "rev-parse", "HEAD"))
            self.assertEqual(
                git(base, "rev-list", "--parents", "-n", "1", "HEAD").count(" "),
                2,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = repository(root)
            transaction = GitWorktreeTransaction.create(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/stale",
                worktree_path=root / "feature",
            )
            (transaction.worktree_path / "feature.txt").write_text(
                "feature\n", encoding="utf-8"
            )
            transaction.commit_candidate(
                allowed_paths=("feature.txt",),
                message="Feature",
            )
            (base / "base-only.txt").write_text("advance\n", encoding="utf-8")
            git(base, "add", "base-only.txt")
            git(base, "commit", "--no-gpg-sign", "-m", "Advance base")

            with self.assertRaisesRegex(GitTransactionError, "base advanced"):
                transaction.integrate(merge=True)

    def test_merge_rejects_wrong_checked_out_base_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = repository(root)
            transaction = GitWorktreeTransaction.create(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/wrong-base",
                worktree_path=root / "feature",
            )
            (transaction.worktree_path / "feature.txt").write_text(
                "feature\n", encoding="utf-8"
            )
            transaction.commit_candidate(
                allowed_paths=("feature.txt",),
                message="Feature",
            )
            git(base, "switch", "-c", "other")

            with self.assertRaisesRegex(
                GitTransactionError,
                "base repository is on other",
            ):
                transaction.integrate(merge=True)


if __name__ == "__main__":
    unittest.main()
