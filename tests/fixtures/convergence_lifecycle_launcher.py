"""Deterministic scripted ``FeatureRun`` launcher fixture for CC-05's
lifecycle proof (``tests/test_convergence_lifecycle.py``, ``build-order-cc-05``).

Stands in for the FeatureRun launcher seat ``scripts/run_plan_graph.py``'s
``--launcher module:callable`` names, and nothing more: for each queued
PlanGraph node it deterministically produces that node's candidate commit in
the temp product repository and returns a ``FeatureRunOutcome`` mapping. It
performs no join, no regression gating, and no scheduling of its own --
``PlanGraph`` itself computes every node's base commit before calling this
launcher and decides graph success or failure from the
``FeatureRunOutcome`` this returns. Only ``NODE_EDITS`` below is specific to
one node id; the rest of this module has no knowledge of what a "repair" or
a "join" is.

``scripts/run_plan_graph.py`` exposes no ``--max-parallelism`` flag, so the
``PlanGraph`` it constructs always runs at the default ``max_parallelism=1``
-- its sequential dispatch path, which composes independent nodes by
chaining each one's base commit to the previous node's own sealed candidate
(regardless of ``depends_on``), not by the git-merge join
(``PlanGraph._join_candidates``/``_base_commit_for_run``) that is only
reachable through the ready-set path PlanGraph selects for
``max_parallelism > 1``. Concretely: this fixture's two file-disjoint
repair nodes still both land in the round's one final candidate, but via
that chained composition -- the second-dispatched repair node receives the
first's own candidate as its base commit, not the plan's shared base commit
-- rather than via a two-parent merge commit. This launcher does not try to
paper over that by fabricating a merge of its own; it simply trusts
whatever base commit PlanGraph hands it for every node, including the
join/regression node.

Node ``verification_argv`` is genuinely exercised, not trusted blindly, by
delegating directly to ``harness_labs.plangraph.plan_graph._run_functionality_test``
-- the same clone/checkout/run helper ``PlanGraph`` itself uses for the
graph's own top-level functionality tests (imported and called directly,
per ``tests/test_plan_graph.py``'s own use of it, rather than copying its
clone/checkout/run sequence a second time here). That also means a node's
declared ``verification_timeout_seconds`` is honored the same way PlanGraph
honors it (via ``FunctionalityCommand.timeout_seconds``), and a timeout
(``subprocess.TimeoutExpired``) or an unrunnable command (``OSError``) is
caught alongside ``_LaunchFailure`` below and reported as a failed node
outcome, rather than escaping this launcher call uncaught.

The repository path is supplied out of band via the
``CONVERGENCE_LIFECYCLE_REPOSITORY`` environment variable: ``FeatureRunRequest``
carries no repository field, and ``scripts/run_plan_graph.py`` resolves
``--repository`` itself in its own process, so this in-process launcher (no
subprocess FeatureRun controller of its own) needs an independent way to
agree with it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from harness_labs.plangraph.plan_graph import FunctionalityCommand, _run_functionality_test

REPOSITORY_ENV_VAR = "CONVERGENCE_LIFECYCLE_REPOSITORY"

#: One scripted find/replace edit per repair node id, applied to the file's
#: content at that node's own base commit. A node id absent from this
#: mapping is treated as a join/regression node: it makes no edit of its
#: own and its candidate is exactly whatever base commit PlanGraph handed
#: it (see the module docstring for what that commit actually is under the
#: shipped CLI's sequential dispatch) -- see ``launch`` below.
NODE_EDITS: Mapping[str, Mapping[str, str]] = {
    "repair-index-title-color": {
        "path": "app/index.html",
        "find": 'data-style-light="color:#ff0000;font-size:24px"',
        "replace": 'data-style-light="color:#111827;font-size:24px"',
    },
    "repair-about-title-color": {
        "path": "app/about.html",
        "find": 'data-style-light="color:#ff0000;font-size:24px"',
        "replace": 'data-style-light="color:#111827;font-size:24px"',
    },
}

#: Fixed commit metadata so a candidate commit this launcher produces is
#: byte-identical across runs given the same base commit and edit --
#: build-order-cc-05's "deterministic scripted launcher fixture".
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "cc05-lifecycle-launcher",
    "GIT_AUTHOR_EMAIL": "cc05-lifecycle-launcher@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "cc05-lifecycle-launcher",
    "GIT_COMMITTER_EMAIL": "cc05-lifecycle-launcher@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


class _LaunchFailure(RuntimeError):
    """Raised internally for any condition that fails this one node's
    outcome; always caught in :func:`launch` and translated into a
    ``status: failed`` mapping -- ``PlanGraph``'s sequential dispatch path
    does not itself guard the launcher call against a raised exception."""


def _repository() -> str:
    repository = os.environ.get(REPOSITORY_ENV_VAR)
    if not repository:
        raise _LaunchFailure(
            f"{REPOSITORY_ENV_VAR} must name the temp product repository "
            "the scripted launcher fixture edits"
        )
    return repository


def _run_git(
    repository: str, *arguments: str, input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", repository, *arguments],
        input=input_bytes, capture_output=True, env=env,
    )
    if completed.returncode != 0:
        raise _LaunchFailure(
            f"git {' '.join(arguments)} failed: "
            f"{completed.stderr.decode(errors='replace').strip()}"
        )
    return completed.stdout


def _apply_scripted_edit(
    repository: str, base_commit: str, edit: Mapping[str, str], node_id: str,
) -> str:
    """One new commit atop ``base_commit`` whose tree differs from it by
    exactly ``edit['path']`` -- the scripted find/replace applied to that
    path's content at ``base_commit``.

    Uses ``git read-tree``/``update-index``/``write-tree`` against a scratch
    ``GIT_INDEX_FILE`` so the repository's own working-tree index (if any)
    is never touched -- this launcher never checks anything out.
    """

    path = edit["path"]
    original = _run_git(repository, "show", f"{base_commit}:{path}")
    find = edit["find"].encode("utf-8")
    replace = edit["replace"].encode("utf-8")
    if find not in original:
        raise _LaunchFailure(
            f"scripted edit marker not found in {path!r} at {base_commit} "
            f"for node {node_id!r}"
        )
    updated = original.replace(find, replace)

    env = dict(os.environ)
    env.update(_COMMIT_ENV)
    index_descriptor, index_path = tempfile.mkstemp(prefix="cc05-lifecycle-index-")
    os.close(index_descriptor)
    env["GIT_INDEX_FILE"] = index_path
    try:
        _run_git(repository, "read-tree", base_commit, env=env)
        blob_hash = _run_git(
            repository, "hash-object", "-w", "--stdin", input_bytes=updated, env=env,
        ).decode().strip()
        _run_git(
            repository, "update-index", "--add", "--cacheinfo", "100644", blob_hash, path,
            env=env,
        )
        tree_hash = _run_git(repository, "write-tree", env=env).decode().strip()
    finally:
        try:
            os.remove(index_path)
        except OSError:
            pass
    return _run_git(
        repository, "commit-tree", tree_hash, "-p", base_commit,
        "-m", f"cc05 scripted repair: {node_id}", env=env,
    ).decode().strip()


def launch(request: Any) -> dict[str, object]:
    """The launcher ``scripts/run_plan_graph.py --launcher`` calls, in
    process, for every queued node (no subprocess FeatureRun controller of
    its own)."""

    outcome_base = {
        "plan_graph_id": request.plan_graph_id,
        "plan_node_id": request.plan_node_id,
        "feature_run_id": request.feature_run_id,
        "run_dir": str(request.run_dir),
    }
    try:
        repository = _repository()
        edit = NODE_EDITS.get(request.plan_node_id)
        if edit is None:
            # Join/regression node: it makes no edit of its own -- whatever
            # PlanGraph decided request.base_commit is (see the module
            # docstring) is this node's candidate unchanged.
            candidate = request.base_commit
            evidence_kind = "join-passthrough"
        else:
            candidate = _apply_scripted_edit(
                repository, request.base_commit, edit, request.plan_node_id,
            )
            evidence_kind = "scripted-file-edit"

        verification_argv = list(request.run.verification_argv)
        if verification_argv:
            command = FunctionalityCommand(
                argv=tuple(verification_argv),
                timeout_seconds=request.run.verification_timeout_seconds,
            )
            try:
                _run_functionality_test(Path(repository), command, candidate)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                return {
                    "status": "failed",
                    "evidence": {"error": f"node verification_argv failed: {exc}"},
                    **outcome_base,
                }
    except _LaunchFailure as exc:
        return {"status": "failed", "evidence": {"error": str(exc)}, **outcome_base}

    return {
        "status": "succeeded",
        "candidate_commit": candidate,
        "evidence": {"kind": evidence_kind},
        **outcome_base,
    }
