"""Deterministic index for verified run metric projections."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Iterable
from harness_labs.run_metrics import project_run_metrics


def build_run_metrics_index(run_dirs: Iterable[Path]) -> dict[str, Any]:
    records, diagnostics = [], []
    for run_dir in sorted((Path(item) for item in run_dirs), key=lambda item: item.name):
        try:
            records.append(project_run_metrics(run_dir))
        except (OSError, ValueError, RuntimeError) as exc:
            diagnostics.append({"run_id": run_dir.name, "message": str(exc)})
    return {"records": records, "diagnostics": diagnostics}
