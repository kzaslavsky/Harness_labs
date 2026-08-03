#!/usr/bin/env python3
"""Survey every registered worktree through one resident parent and a child batch."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs import (
    AttemptRunner,
    AuditActor,
    AuditJournal,
    ChildAuthorization,
    ChildDispatcher,
    CodexAppServerSession,
    CodexReadOnlyWorktreeExecutor,
    InMemoryReferenceStore,
    SessionToolExecutor,
    TaskAttempt,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path)
    parser.add_argument("--max-parallelism", type=int, default=6)
    parser.add_argument(
        "--logs-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "logs" / "runs",
    )
    args = parser.parse_args()
    repository = args.repository.resolve(strict=True)
    worktrees = _registered_worktrees(repository)
    if not worktrees:
        raise SystemExit("repository has no registered worktrees")
    if args.max_parallelism < 1:
        raise SystemExit("--max-parallelism must be positive")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"{timestamp}-parallel-worktree-survey-{uuid4().hex[:8]}"
    audit = AuditJournal(
        args.logs_root / run_id,
        run_id,
        actor=AuditActor("survey-controller", "controller"),
    )
    parent = TaskAttempt(
        attempt_id=f"{run_id}/parent",
        task_ref="task:parent",
        context_ref="context:parent",
        grant_ref="grant:parent",
    )
    references: dict[str, object] = {
        "task:parent": (
            "Survey the purpose and current state of every registered Retinology "
            "worktree. Call spawn_children exactly once with one request for every "
            "role in context.worktrees. Each child is the sole analyst for that "
            "worktree. After the complete ordered batch returns, collate a concise "
            "Markdown report. Specifically test the operator's hypothesis that the "
            "only currently active worktrees with significant unmerged product work "
            "are cdmschema_wtisolated and flow-node-mockup-parity, while almost all "
            "others are aborted benchmark runs or residue from implement-v* and "
            "serial-implement runs. Do not equate ahead-of-main commits with active "
            "work: distinguish patch-unique substantive work, base/orchestration "
            "branches, completed history, and stale run residue. Name disagreements "
            "and uncertainty. State that conclusions are collated from child reports."
        ),
        "context:parent": {
            "repository": str(repository),
            "worktrees": [
                {
                    "role": item["role"],
                    "path": item["path"],
                    "registered_branch": item.get("branch"),
                    "registered_head": item.get("head"),
                }
                for item in worktrees
            ],
        },
        "grant:parent": {
            "capabilities": ["spawn_children"],
            "child_roles": [item["role"] for item in worktrees],
        },
    }
    authorizations: dict[str, ChildAuthorization] = {}
    for item in worktrees:
        role = item["role"]
        path = item["path"]
        references[f"task:{role}"] = (
            f"Determine the purpose and present activity state of {path}. "
            "Bind every conclusion to commands and repository evidence."
        )
        references[f"context:{role}"] = {"worktree": path}
        references[f"grant:{role}"] = {
            "capabilities": ["read_repository"],
            "worktrees": [path],
        }
    store = InMemoryReferenceStore(references)
    for item in worktrees:
        role = item["role"]
        authorizations[role] = ChildAuthorization(
            role=role,
            task_ref=f"task:{role}",
            context_ref=f"context:{role}",
            grant_ref=f"grant:{role}",
            backend_id="codex-exec-read-only",
            capabilities=frozenset({"read_repository"}),
            executor=CodexReadOnlyWorktreeExecutor(
                store,
                timeout_seconds=300,
                audit=audit,
            ),
        )
    registry_artifact = audit.write_artifact(
        "worktree-registry",
        {
            "repository": str(repository),
            "count": len(worktrees),
            "worktrees": worktrees,
            "max_parallelism": args.max_parallelism,
        },
    )
    audit.append(
        "survey_registry_frozen",
        status="succeeded",
        payload={
            "worktree_count": len(worktrees),
            "max_parallelism": args.max_parallelism,
        },
        artifacts=(registry_artifact,),
    )
    dispatcher = ChildDispatcher(
        parent,
        authorizations,
        max_children_per_attempt=len(worktrees),
        audit=audit,
    )
    session = CodexAppServerSession(
        model="gpt-5.6-terra",
        reasoning="low",
        timeout_seconds=300,
        audit=audit,
    )
    result = AttemptRunner().run(
        parent,
        SessionToolExecutor(
            store=store,
            session=session,
            dispatcher=dispatcher,
            max_parallel_children=min(args.max_parallelism, len(worktrees)),
            max_batch_children=len(worktrees),
            require_all_child_roles=True,
            audit=audit,
        ),
    )
    manifest = audit.finalize(
        result.status,
        result={
            "attempt_id": result.attempt_id,
            "status": result.status,
            "payload": dict(result.payload),
            "evidence": list(result.evidence),
        },
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "run_dir": str(audit.run_dir),
                "worktree_count": len(worktrees),
                "status": result.status,
                "report": result.payload.get("text"),
                "manifest_hash": manifest["manifest_hash"],
            },
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 1


def _registered_worktrees(repository: Path) -> list[dict[str, str]]:
    completed = subprocess.run(
        ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    )
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (*completed.stdout.splitlines(), ""):
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["branch"] = "detached"
    records.sort(key=lambda item: item["path"])
    for index, record in enumerate(records, start=1):
        record["role"] = f"worktree_{index:03d}"
    return records


if __name__ == "__main__":
    raise SystemExit(main())
