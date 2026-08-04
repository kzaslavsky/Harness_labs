"""Controller-owned Git worktree, candidate, and optional merge transactions."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


class GitTransactionError(RuntimeError):
    """Raised when a Git transaction cannot preserve its declared invariants."""


def git_output(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GitTransactionError(
            f"git {' '.join(args)} failed with {completed.returncode}: {detail}"
        )
    return completed.stdout.strip()


def changed_paths(repository: Path) -> tuple[str, ...]:
    """Return every tracked or untracked path changed relative to HEAD."""

    tracked = _git_bytes(
        repository,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        "HEAD",
    )
    untracked = _git_bytes(
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    values = {
        item.decode("utf-8")
        for raw in (tracked, untracked)
        for item in raw.split(b"\0")
        if item
    }
    return tuple(sorted(values))


def normalize_allowed_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized = []
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise GitTransactionError("writable paths must be non-empty strings")
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise GitTransactionError(f"writable path escapes repository: {value}")
        text = candidate.as_posix().removeprefix("./").rstrip("/")
        if not text or text == ".git" or text.startswith(".git/"):
            raise GitTransactionError(f"writable path is forbidden: {value}")
        normalized.append(text)
    if len(set(normalized)) != len(normalized):
        raise GitTransactionError("writable paths must be unique")
    return tuple(sorted(normalized))


def paths_outside_scope(
    actual_paths: Iterable[str],
    allowed_paths: Iterable[str],
) -> tuple[str, ...]:
    allowed = normalize_allowed_paths(allowed_paths)
    return tuple(
        sorted(
            path
            for path in actual_paths
            if not any(path == root or path.startswith(root + "/") for root in allowed)
        )
    )


def workspace_snapshot(repository: Path) -> dict[str, Any]:
    repository = repository.resolve(strict=True)
    paths = changed_paths(repository)
    return {
        "head": git_output(repository, "rev-parse", "HEAD"),
        "branch": git_output(repository, "branch", "--show-current"),
        "changed_paths": list(paths),
        "files": {
            path: _path_state(repository / path)
            for path in paths
        },
    }


@dataclass
class GitWorktreeTransaction:
    """Own one isolated feature branch from creation through optional merge."""

    base_repository: Path
    base_branch: str
    feature_branch: str
    worktree_path: Path
    base_commit: str
    candidate_commit: str | None = None

    @classmethod
    def create(
        cls,
        *,
        base_repository: Path,
        base_branch: str,
        feature_branch: str,
        worktree_path: Path,
    ) -> GitWorktreeTransaction:
        base_repository = base_repository.resolve(strict=True)
        worktree_path = worktree_path.resolve()
        if not base_branch.strip() or not feature_branch.strip():
            raise GitTransactionError("branch names must be non-empty")
        git_output(base_repository, "check-ref-format", "--branch", base_branch)
        git_output(base_repository, "check-ref-format", "--branch", feature_branch)
        if worktree_path.exists():
            raise GitTransactionError("feature worktree path already exists")
        top = Path(
            git_output(base_repository, "rev-parse", "--show-toplevel")
        ).resolve()
        if top != base_repository:
            raise GitTransactionError("base_repository must be the Git root")
        current_branch = git_output(
            base_repository, "branch", "--show-current"
        )
        if current_branch != base_branch:
            raise GitTransactionError(
                f"base repository is on {current_branch}, expected {base_branch}"
            )
        if changed_paths(base_repository):
            raise GitTransactionError("base repository must be clean")
        base_commit = git_output(
            base_repository, "rev-parse", f"refs/heads/{base_branch}"
        )
        branch_probe = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{feature_branch}"],
            cwd=base_repository,
            check=False,
        )
        if branch_probe.returncode == 0:
            raise GitTransactionError("feature branch already exists")
        if branch_probe.returncode not in {0, 1}:
            raise GitTransactionError("cannot inspect feature branch")
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        git_output(
            base_repository,
            "worktree",
            "add",
            "-b",
            feature_branch,
            str(worktree_path),
            base_commit,
        )
        transaction = cls(
            base_repository,
            base_branch,
            feature_branch,
            worktree_path,
            base_commit,
        )
        transaction._verify_feature_identity(expected_head=base_commit)
        return transaction

    def creation_receipt(self) -> dict[str, Any]:
        return {
            "protocol": "git-worktree-transaction/1",
            "operation": "create",
            "base_repository": str(self.base_repository),
            "base_branch": self.base_branch,
            "feature_branch": self.feature_branch,
            "worktree_path": str(self.worktree_path),
            "base_commit": self.base_commit,
            "verified_head": git_output(self.worktree_path, "rev-parse", "HEAD"),
        }

    def commit_candidate(
        self,
        *,
        allowed_paths: Iterable[str],
        message: str,
    ) -> dict[str, Any]:
        if not message.strip():
            raise GitTransactionError("candidate commit message must be non-empty")
        self._verify_feature_identity(expected_head=self.base_commit)
        paths = changed_paths(self.worktree_path)
        if not paths:
            raise GitTransactionError("candidate has no repository changes")
        allowed = normalize_allowed_paths(allowed_paths)
        outside = paths_outside_scope(paths, allowed)
        if outside:
            raise GitTransactionError(
                "candidate changed paths outside scope: " + ", ".join(outside)
            )
        before = workspace_snapshot(self.worktree_path)
        git_output(self.worktree_path, "add", "--", *allowed)
        staged = tuple(
            item
            for item in _git_bytes(
                self.worktree_path,
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                "-z",
            ).decode("utf-8").split("\0")
            if item
        )
        if tuple(sorted(staged)) != paths:
            raise GitTransactionError("staged candidate does not match workspace changes")
        git_output(
            self.worktree_path,
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        )
        self.candidate_commit = git_output(
            self.worktree_path, "rev-parse", "HEAD"
        )
        if changed_paths(self.worktree_path):
            raise GitTransactionError("candidate worktree is not clean after commit")
        return {
            "protocol": "git-worktree-transaction/1",
            "operation": "commit",
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "changed_paths": list(paths),
            "allowed_paths": list(allowed),
            "precommit_snapshot": before,
        }

    def integrate(self, *, merge: bool) -> dict[str, Any]:
        if self.candidate_commit is None:
            raise GitTransactionError("candidate must be committed before integration")
        self._verify_feature_identity(expected_head=self.candidate_commit)
        if changed_paths(self.worktree_path):
            raise GitTransactionError("candidate worktree is dirty")
        current_base = git_output(
            self.base_repository, "rev-parse", f"refs/heads/{self.base_branch}"
        )
        if current_base != self.base_commit:
            raise GitTransactionError(
                f"base advanced from {self.base_commit} to {current_base}"
            )
        if not merge:
            return {
                "protocol": "git-worktree-transaction/1",
                "operation": "integrate",
                "status": "ready_not_merged",
                "base_commit": self.base_commit,
                "candidate_commit": self.candidate_commit,
                "merge_commit": None,
            }
        checked_out_base = git_output(
            self.base_repository, "branch", "--show-current"
        )
        if checked_out_base != self.base_branch:
            raise GitTransactionError(
                f"base repository is on {checked_out_base}, "
                f"expected {self.base_branch}"
            )
        if changed_paths(self.base_repository):
            raise GitTransactionError("base repository is dirty")
        git_output(
            self.base_repository,
            "merge-tree",
            "--write-tree",
            self.base_commit,
            self.candidate_commit,
        )
        git_output(
            self.base_repository,
            "merge",
            "--no-ff",
            "--no-edit",
            self.candidate_commit,
        )
        merge_commit = git_output(self.base_repository, "rev-parse", "HEAD")
        if (
            git_output(
                self.base_repository,
                "merge-base",
                "--is-ancestor",
                self.candidate_commit,
                merge_commit,
            )
            != ""
        ):
            raise GitTransactionError("candidate is not reachable from merge commit")
        if changed_paths(self.base_repository):
            raise GitTransactionError("base repository is dirty after merge")
        return {
            "protocol": "git-worktree-transaction/1",
            "operation": "integrate",
            "status": "merged",
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "merge_commit": merge_commit,
            "verified_base_head": git_output(
                self.base_repository,
                "rev-parse",
                f"refs/heads/{self.base_branch}",
            ),
        }

    def _verify_feature_identity(self, *, expected_head: str) -> None:
        if not self.worktree_path.is_dir():
            raise GitTransactionError("feature worktree is missing")
        branch = git_output(self.worktree_path, "branch", "--show-current")
        if branch != self.feature_branch:
            raise GitTransactionError(
                f"feature worktree is on {branch}, expected {self.feature_branch}"
            )
        head = git_output(self.worktree_path, "rev-parse", "HEAD")
        if head != expected_head:
            raise GitTransactionError(
                f"feature worktree HEAD is {head}, expected {expected_head}"
            )


def _git_bytes(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitTransactionError(
            f"git {' '.join(args)} failed with {completed.returncode}: {detail}"
        )
    return completed.stdout


def _path_state(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"kind": "deleted", "sha256": None}
    if path.is_symlink():
        target = os.readlink(path)
        return {
            "kind": "symlink",
            "sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        }
    if path.is_file():
        return {
            "kind": "file",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    return {"kind": "directory", "sha256": None}


__all__ = [
    "GitTransactionError",
    "GitWorktreeTransaction",
    "changed_paths",
    "git_output",
    "normalize_allowed_paths",
    "paths_outside_scope",
    "workspace_snapshot",
]
