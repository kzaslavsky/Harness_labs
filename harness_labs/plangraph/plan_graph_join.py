"""Join-conflict resolution registry for PlanGraph sibling joins.

``PlanGraph._join_candidates`` performs controller-owned mechanical merges
between sealed sibling candidates.  A real merge conflict there is a plan
defect — the siblings' allowed paths were not disjoint in effect — and the
join step must never invent a resolution on its own.  This module is the
narrow, auditable channel through which an operator (or an agent acting on an
operator's behalf) who has *actually diagnosed* the conflict hands a verified
resolution to that mechanical step:

* :func:`describe_join_conflict` reproduces the conflict with
  ``git merge-tree --write-tree`` and returns a complete, structured
  description (parents, merge bases, conflicted paths, marker-laden content,
  and a content-derived ``resolution_key``).
* :class:`JoinConflictResolutionStore` is an append-only, advisory-locked,
  fsynced JSONL journal (the ``RetryBudgetLedger`` shape) recording verified
  resolutions.  Registration re-runs the merge itself and rejects any
  resolved tree that is not a genuine resolution of the observed conflict.
* ``PlanGraph._join_candidates`` consults the store on conflict; with a valid
  registration it commits the resolved tree with both parents exactly as a
  clean merge would have, otherwise it raises with full diagnostics and a
  durable conflict artifact.

Keying is content-derived — the two parents' *tree* ids plus the merge-base
trees, not the parent commit ids — because synthetic intermediate join
commits are re-created per attempt with fresh timestamps and therefore have
unstable commit ids, while the merge inputs (trees and bases) are stable.
The observed commit ids are still recorded as provenance.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


JOIN_RESOLUTION_PROTOCOL = "join-conflict-resolution/1"

# Cap for embedded marker-laden file content in conflict descriptions and
# artifacts.  Large files stay diagnosable through the recorded automerge
# tree id (``git show <tree>:<path>``); the artifact stays readable.
_CONTENT_EMBED_LIMIT = 65536


class JoinResolutionError(ValueError):
    pass


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, text=True,
        capture_output=True, check=False,
    )
    if completed.returncode:
        raise JoinResolutionError(
            f"git {' '.join(arguments[:2])} failed: {completed.stderr.strip()[:400]}"
        )
    return completed.stdout


def _resolve_commit(repository: Path, name: str, role: str) -> str:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", f"{name}^{{commit}}"],
        cwd=repository, text=True, capture_output=True, check=False,
    )
    if probe.returncode:
        raise JoinResolutionError(f"{role} {name!r} is not a commit in {repository}")
    return probe.stdout.strip()


def _tree_of(repository: Path, commit: str) -> str:
    return _git(repository, "rev-parse", f"{commit}^{{tree}}").strip()


def _resolution_key(
    label: str, parent_trees: list[str], merge_base_trees: list[str]
) -> str:
    shape = {
        "protocol": JOIN_RESOLUTION_PROTOCOL,
        "label": label,
        "parent_trees": parent_trees,
        "merge_base_trees": sorted(merge_base_trees),
    }
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _blob_text(repository: Path, tree: str, path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f"{tree}:{path}"],
        cwd=repository, capture_output=True, check=False,
    )
    if completed.returncode:
        return ""
    data = completed.stdout
    truncated = len(data) > _CONTENT_EMBED_LIMIT
    text = data[:_CONTENT_EMBED_LIMIT].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n[... truncated at {_CONTENT_EMBED_LIMIT} bytes ...]\n"
    return text


def _retains_conflict_markers(repository: Path, tree: str, path: str) -> bool:
    completed = subprocess.run(
        ["git", "show", f"{tree}:{path}"],
        cwd=repository, capture_output=True, check=False,
    )
    if completed.returncode:
        # Path absent from the resolved tree: a deletion is a deliberate
        # resolution, not retained markers.
        return False
    starts = {line[:8] for line in completed.stdout.split(b"\n")}
    has_ours = any(start.startswith(b"<<<<<<< ") for start in starts)
    has_theirs = any(start.startswith(b">>>>>>> ") for start in starts)
    return has_ours and has_theirs


def describe_join_conflict(
    repository: Path, label: str, parent_a: str, parent_b: str
) -> dict[str, Any]:
    """Reproduce and fully describe the merge conflict between two candidates.

    Raises :class:`JoinResolutionError` when the parents do not actually
    conflict — a caller must never describe (or later register a resolution
    for) a pair that merges cleanly.
    """
    repository = Path(repository).resolve()
    if not isinstance(label, str) or not label:
        raise JoinResolutionError("join label must be a non-empty string")
    commit_a = _resolve_commit(repository, parent_a, "join parent")
    commit_b = _resolve_commit(repository, parent_b, "join parent")
    if commit_a == commit_b:
        raise JoinResolutionError("join parents are the same commit")
    merge = subprocess.run(
        ["git", "merge-tree", "--write-tree", commit_a, commit_b],
        cwd=repository, text=True, capture_output=True, check=False,
    )
    if merge.returncode == 0:
        raise JoinResolutionError(
            f"join {label!r} parents {commit_a[:12]} and {commit_b[:12]} merge "
            "cleanly; there is no conflict to describe or resolve"
        )
    if merge.returncode != 1 or not merge.stdout.strip():
        raise JoinResolutionError(
            f"git merge-tree failed for join {label!r}: "
            + (merge.stderr or merge.stdout).strip()[:400]
        )
    lines = merge.stdout.splitlines()
    automerge_tree = lines[0].strip()
    conflicted_paths: list[str] = []
    stages: dict[str, list[dict[str, str]]] = {}
    informational_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if not line.strip():
            informational_start = index + 1
            break
        # "<mode> <object> <stage>\t<path>"
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or not path:
            informational_start = index
            break
        if path not in stages:
            stages[path] = []
            conflicted_paths.append(path)
        stages[path].append({"mode": parts[0], "object": parts[1], "stage": parts[2]})
    informational = "\n".join(lines[informational_start:]).strip()
    if not conflicted_paths:
        raise JoinResolutionError(
            f"git merge-tree reported a conflict for join {label!r} but listed "
            "no conflicted files; refusing to proceed on an unparseable result"
        )
    parent_trees = [_tree_of(repository, commit_a), _tree_of(repository, commit_b)]
    merge_base_trees = sorted(
        {
            _tree_of(repository, base)
            for base in _git(
                repository, "merge-base", "--all", commit_a, commit_b
            ).split()
        }
    )
    return {
        "protocol": JOIN_RESOLUTION_PROTOCOL,
        "label": label,
        "parents": [commit_a, commit_b],
        "parent_trees": parent_trees,
        "merge_base_trees": merge_base_trees,
        "resolution_key": _resolution_key(label, parent_trees, merge_base_trees),
        "automerge_tree": automerge_tree,
        "conflicted_paths": conflicted_paths,
        "conflict_stages": stages,
        "conflicted_files": {
            path: _blob_text(repository, automerge_tree, path)
            for path in conflicted_paths
        },
        "merge_tree_output": merge.stdout,
        "informational": informational,
    }


class JoinConflictResolutionStore:
    """Append-only registry of verified resolutions for join conflicts.

    One JSONL journal per plan lineage under
    ``<run_root>/.plan-graph-join-resolutions/<lineage_id>.jsonl``; every
    mutation is serialized by an advisory lock and fsynced before return,
    following the ``RetryBudgetLedger`` durability pattern.  Sequencing is
    the journal order itself (explicit monotonic ``sequence``), never wall
    clocks.
    """

    protocol = "join-conflict-resolution-store/1"

    def __init__(self, run_root: Path, lineage_id: str, repository: Path) -> None:
        if not isinstance(lineage_id, str) or not lineage_id or any(
            character in lineage_id for character in "/\\"
        ):
            raise JoinResolutionError("lineage_id must be a non-empty path-safe name")
        self.path = (
            Path(run_root).resolve()
            / ".plan-graph-join-resolutions"
            / f"{lineage_id}.jsonl"
        )
        self.lineage_id = lineage_id
        self.repository = Path(repository).resolve()

    # -- registration -----------------------------------------------------

    def register(
        self,
        *,
        label: str,
        parent_a: str,
        parent_b: str,
        resolved_tree: str,
        reason: str,
        actor: str = "operator",
        supersede: bool = False,
    ) -> dict[str, Any]:
        """Record a verified resolution for one observed join conflict.

        The registration re-derives the conflict itself and fail-closes:

        * the pair must *really* conflict right now (``describe_join_conflict``
          raises on a clean merge, so an unrelated pair cannot be registered);
        * ``resolved_tree`` must exist as a tree object in the repository;
        * it may differ from the mechanical automerge tree only at the
          conflicted paths — every auto-merged path is preserved bit-for-bit,
          so an unrelated tree cannot be smuggled in through this channel;
        * it must change at least one conflicted path, and no conflicted path
          may retain conflict markers.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise JoinResolutionError("a resolution registration requires a reason")
        if not isinstance(actor, str) or not actor.strip():
            raise JoinResolutionError("a resolution registration requires an actor")
        description = describe_join_conflict(
            self.repository, label, parent_a, parent_b
        )
        resolved = self._verify_resolved_tree(description, resolved_tree)
        with self._locked() as handle:
            state = self._fold(handle)
            key = description["resolution_key"]
            existing = state["resolutions"].get(key)
            if existing is not None and not supersede:
                if existing["resolved_tree"] == resolved:
                    return existing  # idempotent re-registration
                raise JoinResolutionError(
                    f"join {label!r} already has resolution sequence "
                    f"{existing['sequence']} for this conflict "
                    f"(resolved tree {existing['resolved_tree'][:12]}); pass "
                    "supersede=True to record a replacement"
                )
            record = {
                "event": "registered",
                "sequence": state["next_sequence"],
                "label": label,
                "parents": description["parents"],
                "parent_trees": description["parent_trees"],
                "merge_base_trees": description["merge_base_trees"],
                "resolution_key": key,
                "automerge_tree": description["automerge_tree"],
                "conflicted_paths": description["conflicted_paths"],
                "resolved_tree": resolved,
                "reason": reason,
                "actor": actor,
                "supersedes": existing["sequence"] if existing else None,
            }
            self._append(handle, record)
        # Anchor the resolved tree against gc for the lineage's lifetime.
        _git(
            self.repository, "update-ref",
            f"refs/plan-graph-join/{self.lineage_id}/{key[:16]}", resolved,
        )
        return {key_: value for key_, value in record.items() if key_ != "event"}

    def _verify_resolved_tree(
        self, description: Mapping[str, Any], resolved_tree: str
    ) -> str:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", f"{resolved_tree}^{{tree}}"],
            cwd=self.repository, text=True, capture_output=True, check=False,
        )
        if probe.returncode:
            raise JoinResolutionError(
                f"resolved tree {resolved_tree!r} is not a tree object in "
                f"{self.repository}"
            )
        resolved = probe.stdout.strip()
        automerge_tree = description["automerge_tree"]
        conflicted = set(description["conflicted_paths"])
        changed = [
            line
            for line in _git(
                self.repository, "diff-tree", "-r", "--name-only",
                "--no-commit-id", automerge_tree, resolved,
            ).splitlines()
            if line
        ]
        if not changed:
            raise JoinResolutionError(
                "resolved tree is identical to the mechanical automerge tree; "
                "the conflicted files still carry conflict markers, so this "
                "is not a resolution"
            )
        stray = sorted(set(changed) - conflicted)
        if stray:
            raise JoinResolutionError(
                "resolved tree modifies paths outside the observed conflict "
                f"({stray[:10]}); a join resolution may only change the "
                f"conflicted paths {sorted(conflicted)}"
            )
        retained = sorted(
            path
            for path in conflicted
            if _retains_conflict_markers(self.repository, resolved, path)
        )
        if retained:
            raise JoinResolutionError(
                f"resolved tree still contains conflict markers in {retained}; "
                "resolve the conflict content before registering"
            )
        return resolved

    # -- lookup / use -----------------------------------------------------

    def lookup(
        self, *, label: str, parent_a: str, parent_b: str
    ) -> dict[str, Any] | None:
        """Return the active registered resolution for this pair, if any.

        Returns ``None`` both when nothing is registered and when the pair
        does not conflict (a clean merge needs no resolution).
        """
        repository = self.repository
        try:
            commit_a = _resolve_commit(repository, parent_a, "join parent")
            commit_b = _resolve_commit(repository, parent_b, "join parent")
            parent_trees = [
                _tree_of(repository, commit_a), _tree_of(repository, commit_b)
            ]
            merge_base_trees = sorted(
                {
                    _tree_of(repository, base)
                    for base in _git(
                        repository, "merge-base", "--all", commit_a, commit_b
                    ).split()
                }
            )
        except JoinResolutionError:
            return None
        key = _resolution_key(label, parent_trees, merge_base_trees)
        if not self.path.exists():
            return None
        with self._locked(shared=True) as handle:
            state = self._fold(handle)
        return state["resolutions"].get(key)

    def resolve(
        self, *, label: str, parent_a: str, parent_b: str
    ) -> dict[str, Any] | None:
        """Return a *re-verified* resolution for use by a mechanical join.

        ``None`` means no resolution is registered and the caller must raise
        as before.  A registered resolution is revalidated against the live
        repository (the conflict is re-derived and the resolved tree is
        re-checked); a registration that no longer verifies raises loudly
        rather than being silently ignored, because it indicates tampering or
        repository drift that an operator must see.
        """
        record = self.lookup(label=label, parent_a=parent_a, parent_b=parent_b)
        if record is None:
            return None
        description = describe_join_conflict(
            self.repository, label, parent_a, parent_b
        )
        if description["resolution_key"] != record["resolution_key"]:
            raise JoinResolutionError(
                f"registered resolution sequence {record['sequence']} for join "
                f"{label!r} no longer matches the live conflict identity"
            )
        self._verify_resolved_tree(description, record["resolved_tree"])
        return record

    def records(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        with self._locked(shared=True) as handle:
            state = self._fold(handle)
        return tuple(state["journal"])

    # -- journal mechanics (RetryBudgetLedger durability pattern) ---------

    def _locked(self, shared: bool = False) -> "_Lock":
        directory = self.path.parent
        directory_was_missing = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if directory_was_missing:
            _fsync_directory(directory.parent)
        journal_was_missing = not self.path.exists()
        handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        return _Lock(handle, journal_was_missing=journal_was_missing)

    def _fold(self, handle: "_Lock") -> dict[str, Any]:
        state: dict[str, Any] = {
            "resolutions": {}, "journal": [], "next_sequence": 1,
        }
        handle.seek(0)
        for line in handle:
            try:
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or event.get("protocol") != self.protocol
                    or event.get("lineage_id") != self.lineage_id
                    or event.get("event") != "registered"
                    or event.get("sequence") != state["next_sequence"]
                ):
                    raise ValueError
                for field in (
                    "label", "resolution_key", "resolved_tree",
                    "automerge_tree", "reason", "actor",
                ):
                    if not isinstance(event.get(field), str) or not event[field]:
                        raise ValueError
                if (
                    not isinstance(event.get("parents"), list)
                    or len(event["parents"]) != 2
                    or not isinstance(event.get("conflicted_paths"), list)
                    or not event["conflicted_paths"]
                ):
                    raise ValueError
                record = {
                    key: value for key, value in event.items()
                    if key not in {"protocol", "lineage_id", "event"}
                }
                state["resolutions"][event["resolution_key"]] = record
                state["journal"].append(record)
                state["next_sequence"] += 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise JoinResolutionError(
                    "join-resolution journal is corrupt; operator intervention "
                    "required"
                ) from exc
        return state

    def _append(self, handle: "_Lock", event: dict[str, Any]) -> None:
        payload = {"protocol": self.protocol, "lineage_id": self.lineage_id, **event}
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if handle.journal_was_missing:
            _fsync_directory(self.path.parent)
            handle.journal_was_missing = False


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class _Lock:
    def __init__(self, handle, *, journal_was_missing: bool):
        self.handle = handle
        self.journal_was_missing = journal_was_missing

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __iter__(self):
        return iter(self.handle)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


# -- operator CLI ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m harness_labs.plangraph.plan_graph_join",
        description=(
            "Inspect and register verified resolutions for PlanGraph join "
            "conflicts."
        ),
    )
    parser.add_argument("--repository", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    describe = subparsers.add_parser(
        "describe", help="print the structured conflict description as JSON"
    )
    describe.add_argument("label")
    describe.add_argument("parent_a")
    describe.add_argument("parent_b")

    register = subparsers.add_parser(
        "register", help="register a verified resolved tree for a conflict"
    )
    register.add_argument("--run-root", required=True, type=Path)
    register.add_argument("--lineage-id", required=True)
    register.add_argument("--resolved-tree", required=True)
    register.add_argument("--reason", required=True)
    register.add_argument("--actor", default="operator")
    register.add_argument("--supersede", action="store_true")
    register.add_argument("label")
    register.add_argument("parent_a")
    register.add_argument("parent_b")

    list_parser = subparsers.add_parser(
        "list", help="print every journaled resolution for a lineage"
    )
    list_parser.add_argument("--run-root", required=True, type=Path)
    list_parser.add_argument("--lineage-id", required=True)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "describe":
            print(json.dumps(
                describe_join_conflict(
                    arguments.repository, arguments.label,
                    arguments.parent_a, arguments.parent_b,
                ),
                indent=2, sort_keys=True,
            ))
        elif arguments.command == "register":
            store = JoinConflictResolutionStore(
                arguments.run_root, arguments.lineage_id, arguments.repository
            )
            record = store.register(
                label=arguments.label,
                parent_a=arguments.parent_a,
                parent_b=arguments.parent_b,
                resolved_tree=arguments.resolved_tree,
                reason=arguments.reason,
                actor=arguments.actor,
                supersede=arguments.supersede,
            )
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            store = JoinConflictResolutionStore(
                arguments.run_root, arguments.lineage_id, arguments.repository
            )
            print(json.dumps(list(store.records()), indent=2, sort_keys=True))
    except JoinResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
