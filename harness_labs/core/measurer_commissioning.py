"""Measurer commissioning (DTR-F4): pre-campaign calibration, core-layer.

Core-layer module: plain JSON in, plain JSON out. It imports nothing from
``harness_labs.plangraph`` (``tests/test_import_boundaries.py`` enforces the
boundary via ``scripts/dev/check_import_boundaries.py``'s directory-derived
layering -- no checker edit is needed for a new file under
``harness_labs/core``). Seed findings arrive as plain mappings supplied by
path, never as plangraph types; sealing and checklist wiring live entirely
in ``scripts/commission_measurer.py`` and
``harness_labs/plangraph/convergence_campaign.py``.

Two independent calibrations, run before a campaign opens:

* **Stability** (:func:`build_stability_report`): the capture matrix is run
  ``runs`` times through an injected ``runner`` callable -- the same
  ``runner(attempt) -> {cell_id: value}`` seam the driver's own
  ``measure`` step already resolves capture through, kept abstract here so
  this module never has to know what a "cell" or a "value" actually is.
  Each cell's divergence (the fraction of runs departing from its modal
  value) is classified against a declared threshold, and the threshold
  itself is recorded in the report so it is never implicit. Commissioning
  cannot report silent success while a cell is unstable and unruled: an
  unstable cell surfaces as an explicit ruling request, and only a recorded
  ruling (``excluded`` or ``threshold_amended``, each with a non-empty
  ``reason``) resolves it -- :func:`stability_exit_code` stays nonzero
  until every unstable cell carries one.
* **Recall** (:func:`score_inspector_recall`): an injected ``inspector``
  callable is scored against a seed-findings list -- the exact envelope
  shape ``harness_labs.plangraph.finding_intake``'s ``seal_findings``
  builds and ``scripts/report_finding.py --batch`` emits (a mapping with a
  ``findings`` list of ``file``/``subject``-keyed entries), read here purely
  as data via :func:`load_seed_findings`.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "MeasurerCommissioningError",
    "STABILITY_REPORT_PROTOCOL",
    "RECALL_REPORT_PROTOCOL",
    "build_stability_report",
    "stability_exit_code",
    "load_seed_findings",
    "score_inspector_recall",
]

STABILITY_REPORT_PROTOCOL = "measurer-commissioning-stability-report/1"
RECALL_REPORT_PROTOCOL = "measurer-commissioning-recall-report/1"

_VALID_DISPOSITIONS = frozenset({"excluded", "threshold_amended"})


class MeasurerCommissioningError(ValueError):
    """Raised on a malformed commissioning input (matrix, runner result,
    ruling, or seed-findings envelope)."""


# -- stability ----------------------------------------------------------


def _validate_unit_interval(value: Any, *, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not (
        0.0 <= float(value) <= 1.0
    ):
        raise MeasurerCommissioningError(f"{label} must be a number between 0 and 1")
    return float(value)


def _validate_ruling(cell: str, ruling: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(ruling, Mapping):
        raise MeasurerCommissioningError(f"ruling for cell {cell!r} must be a mapping")
    disposition = ruling.get("disposition")
    if disposition not in _VALID_DISPOSITIONS:
        raise MeasurerCommissioningError(
            f"ruling for cell {cell!r} disposition must be one of "
            f"{sorted(_VALID_DISPOSITIONS)}, got {disposition!r}"
        )
    reason = ruling.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise MeasurerCommissioningError(
            f"ruling for cell {cell!r} must carry a non-empty 'reason'"
        )
    validated: dict[str, Any] = {"disposition": disposition, "reason": reason}
    if disposition == "threshold_amended":
        validated["amended_threshold"] = _validate_unit_interval(
            ruling.get("amended_threshold"),
            label=f"ruling for cell {cell!r} 'amended_threshold'",
        )
    return validated


def build_stability_report(
    capture_matrix: Sequence[str],
    *,
    runs: int,
    runner: Callable[[int], Mapping[str, Any]],
    divergence_threshold: float,
    rulings: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run ``capture_matrix`` through ``runner`` ``runs`` times and classify
    each cell stable/unstable against ``divergence_threshold``.

    ``runner(attempt)`` is called once per attempt (``0`` .. ``runs - 1``)
    and must return a mapping covering every cell in ``capture_matrix`` --
    the same per-attempt seam a real capture-driven caller resolves via a
    subprocess invocation of the stub or real capture driver
    (``scripts/commission_measurer.py``), kept abstract here so this module
    never has to parse a receipt shape.

    A cell's divergence is ``1 - (modal_count / runs)``: the fraction of
    attempts departing from its most common value. ``divergence_threshold``
    is recorded verbatim in the report (never left implicit). A cell whose
    divergence exceeds the threshold is "unstable"; it stays unstable in the
    report's own ``cells`` entry regardless of ``rulings`` -- a ruling
    resolves whether commissioning may report success
    (:func:`stability_exit_code`), not the underlying measurement.

    ``rulings`` maps a cell id to ``{"disposition": "excluded" |
    "threshold_amended", "reason": <non-empty str>}`` (a ``threshold_amended``
    ruling also carries a numeric ``amended_threshold``). Every ruling is
    validated and recorded as data in the report's own ``rulings`` key,
    whether or not the cell it names is currently unstable. An ``excluded``
    ruling always resolves the cell it names; a ``threshold_amended`` ruling
    only resolves it when ``amended_threshold`` actually covers the cell's
    observed divergence -- an amendment that does not cover the divergence
    it was meant to excuse leaves the cell in ``unruled_unstable_cells`` and
    it keeps blocking success.
    """

    if not capture_matrix:
        raise MeasurerCommissioningError("capture_matrix must not be empty")
    if len(set(capture_matrix)) != len(capture_matrix):
        raise MeasurerCommissioningError("capture_matrix must not repeat a cell id")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
        raise MeasurerCommissioningError("runs must be a positive integer")
    divergence_threshold = _validate_unit_interval(
        divergence_threshold, label="divergence_threshold"
    )

    samples: dict[str, list[Any]] = {cell: [] for cell in capture_matrix}
    for attempt in range(runs):
        result = runner(attempt)
        if not isinstance(result, Mapping):
            raise MeasurerCommissioningError(
                f"runner(attempt={attempt}) must return a mapping of cell id to value"
            )
        for cell in capture_matrix:
            if cell not in result:
                raise MeasurerCommissioningError(
                    f"runner(attempt={attempt}) result is missing cell {cell!r}"
                )
            samples[cell].append(result[cell])

    validated_rulings: dict[str, dict[str, Any]] = {
        cell: _validate_ruling(cell, ruling) for cell, ruling in (rulings or {}).items()
    }

    cells: dict[str, Any] = {}
    unstable_cells: list[str] = []
    for cell in capture_matrix:
        values = samples[cell]
        counts = Counter(_hashable(value) for value in values)
        modal_count = counts.most_common(1)[0][1]
        divergence = 1.0 - (modal_count / len(values))
        stable = divergence <= divergence_threshold
        cells[cell] = {
            "samples": list(values),
            "distinct_values": len(counts),
            "divergence": divergence,
            "stable": stable,
        }
        if not stable:
            unstable_cells.append(cell)

    unruled_unstable_cells = [
        cell for cell in unstable_cells if not _ruling_resolves_cell(
            validated_rulings.get(cell), cells[cell]["divergence"],
        )
    ]
    ruling_requests = [
        {
            "cell": cell,
            "divergence": cells[cell]["divergence"],
            "message": (
                f"cell {cell!r} divergence {cells[cell]['divergence']:.4f} exceeds "
                f"threshold {divergence_threshold}; commissioning requires an "
                "explicit ruling ('excluded' or 'threshold_amended', with a "
                "recorded reason) before it can report success"
            ),
        }
        for cell in unruled_unstable_cells
    ]

    return {
        "protocol": STABILITY_REPORT_PROTOCOL,
        "divergence_threshold": divergence_threshold,
        "runs": runs,
        "capture_matrix": list(capture_matrix),
        "cells": cells,
        "unstable_cells": unstable_cells,
        "rulings": validated_rulings,
        "ruling_requests": ruling_requests,
        "unruled_unstable_cells": unruled_unstable_cells,
        "success": not unruled_unstable_cells,
    }


def stability_exit_code(report: Mapping[str, Any]) -> int:
    """``0`` when every cell is stable or ruled, ``1`` while any chronically
    unstable cell remains unruled -- commissioning never reports silent
    success by exit code alone."""

    return 0 if report.get("success") else 1


def _ruling_resolves_cell(ruling: Mapping[str, Any] | None, divergence: float) -> bool:
    """Whether a validated ruling actually resolves an unstable cell.

    ``excluded`` always resolves the cell -- it is a blanket exclusion,
    independent of the observed divergence. ``threshold_amended`` only
    resolves the cell when its ``amended_threshold`` actually covers the
    observed ``divergence``; an amendment that does not cover the
    divergence it was meant to excuse is not a real disposition, so the
    cell stays in ``unruled_unstable_cells`` and commissioning keeps
    requesting a ruling for it.
    """

    if ruling is None:
        return False
    if ruling["disposition"] == "threshold_amended":
        return divergence <= ruling["amended_threshold"]
    return True


def _hashable(value: Any) -> Any:
    """A canonical, hashable form of a runner-supplied cell value, so a
    dict/list value (e.g. a structured receipt fragment) can still be
    counted for modal divergence without requiring every caller's value to
    already be a hashable scalar."""

    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


# -- recall ---------------------------------------------------------------


def load_seed_findings(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Read a seed-findings file: the sealed audit-artifact envelope
    ``harness_labs.plangraph.finding_intake.seal_findings`` builds and
    ``scripts/report_finding.py --batch`` emits -- ``{"digest": ...,
    "findings": [...], "verdicts": [], "confirmed_good": [],
    "capture_coverage": {}}``. Consumed purely as data: this module never
    imports the plangraph-layer module that produces it.
    """

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or not isinstance(raw.get("findings"), list):
        raise MeasurerCommissioningError(
            f"seed-findings file {path} must hold a finding_intake --batch "
            "envelope: a mapping with a 'findings' list"
        )
    return tuple(raw["findings"])


def score_inspector_recall(
    seed_findings: Sequence[Mapping[str, Any]],
    *,
    inspector: Callable[[Sequence[Mapping[str, Any]]], Sequence[Sequence[str]]],
) -> dict[str, Any]:
    """Score ``inspector`` against ``seed_findings``'s ``(file, subject)``
    keys and emit a recall report.

    ``inspector`` is called once with the full seed-findings list and must
    return the ``[file, subject]`` pairs it recovers -- the same shape a
    real inspector would report having independently found. Recall is the
    fraction of the seed's distinct keys the inspector recovered.
    """

    if not seed_findings:
        raise MeasurerCommissioningError("seed_findings must not be empty")

    seed_keys: list[tuple[str, str]] = []
    for index, finding in enumerate(seed_findings):
        if not isinstance(finding, Mapping):
            raise MeasurerCommissioningError(f"seed finding {index} must be a mapping")
        file = finding.get("file")
        subject = finding.get("subject")
        if not isinstance(file, str) or not file or not isinstance(subject, str) or not subject:
            raise MeasurerCommissioningError(
                f"seed finding {index} must carry non-empty string 'file' and "
                "'subject' fields"
            )
        key = (file, subject)
        if key not in seed_keys:
            seed_keys.append(key)

    recovered = inspector(list(seed_findings))
    if not isinstance(recovered, Sequence) or isinstance(recovered, (str, bytes)):
        raise MeasurerCommissioningError(
            "inspector must return a sequence of [file, subject] pairs"
        )
    recovered_keys: set[tuple[str, str]] = set()
    for item in recovered:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise MeasurerCommissioningError(
                f"inspector result entries must be [file, subject] string pairs, got {item!r}"
            )
        recovered_keys.add((item[0], item[1]))

    matched = [list(key) for key in seed_keys if key in recovered_keys]
    missed = [list(key) for key in seed_keys if key not in recovered_keys]
    recall = len(matched) / len(seed_keys)

    return {
        "protocol": RECALL_REPORT_PROTOCOL,
        "seed_count": len(seed_keys),
        "matched": matched,
        "missed": missed,
        "recall": recall,
    }
