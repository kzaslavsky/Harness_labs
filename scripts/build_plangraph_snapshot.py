#!/usr/bin/env python3
"""Offline PlanGraph metrics-snapshot builder and historical backfill CLI.

Read-only over run directories except for the snapshots it writes under
``<run-root>/.plan-graph-snapshots/`` -- it never touches a run directory's
own journal, checkpoint, or manifest.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.core.audit import AuditError
from harness_labs.observability.plangraph_snapshot import SnapshotSkipped, build_snapshot, write_snapshot
from harness_labs.observability.run_catalog import build_run_catalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True, help="run root to scan (e.g. logs/runs)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--graph", help="build a snapshot for one graph run_id")
    target.add_argument("--all-completed", action="store_true", help="build snapshots for every terminal PlanGraph under --run-root")
    parser.add_argument("--repository", type=Path, help="repository checkout used for git-derived delta and digest-checked criteria text")
    parser.add_argument("--output-dir", type=Path, help="override the default <run-root>/.plan-graph-snapshots output directory")
    parser.add_argument("--force", action="store_true", help="overwrite an existing snapshot file")
    parser.add_argument("--dry-run", action="store_true", help="report reconstructed/skipped/failed counts without writing")
    parser.add_argument("--include-interrupted", action="store_true", help="also snapshot graphs whose terminal status is 'interrupted'")
    return parser


def _graph_ids(run_root: Path, explicit: str | None) -> list[str]:
    if explicit is not None:
        return [explicit]
    catalog = build_run_catalog(run_root)
    return sorted(item["run_id"] for item in catalog.get("plan_graphs", []) if isinstance(item.get("run_id"), str))


def main() -> int:
    arguments = _parser().parse_args()
    run_root = arguments.run_root.resolve()
    repository = arguments.repository.resolve() if arguments.repository else None
    graph_ids = _graph_ids(run_root, arguments.graph)

    reconstructed: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for graph_id in graph_ids:
        try:
            snapshot = build_snapshot(
                run_root, graph_id, repository=repository,
                include_interrupted=arguments.include_interrupted, reconstructed=True,
            )
        except SnapshotSkipped as exc:
            skipped.append({"graph_id": graph_id, "reason": str(exc)})
            continue
        except (AuditError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failed.append({"graph_id": graph_id, "reason": str(exc)})
            continue
        if arguments.dry_run:
            reconstructed.append(graph_id)
            continue
        try:
            write_snapshot(run_root, snapshot, output_dir=arguments.output_dir, force=arguments.force)
        except (SnapshotSkipped, OSError) as exc:
            failed.append({"graph_id": graph_id, "reason": str(exc)})
            continue
        reconstructed.append(graph_id)

    report = {
        "run_root": str(run_root),
        "dry_run": arguments.dry_run,
        "reconstructed": len(reconstructed),
        "skipped": len(skipped),
        "failed": len(failed),
        "reconstructed_graph_ids": reconstructed,
        "skipped_details": skipped,
        "failed_details": failed,
    }
    print(json.dumps(report, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
