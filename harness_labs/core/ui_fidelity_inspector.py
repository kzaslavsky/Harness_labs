"""CC-03 inspector role module: mandatory per-key verdict validation.

Core-layer module: it imports only ``harness_labs.core.convergence_contract``
and never ``harness_labs.plangraph``, so the closed verdict vocabulary is
read from the one place it is defined
(``tests/test_import_boundaries.py`` enforces the boundary).

Per ``contracts-verdicts``, every audit must return, for every prior
``open``/``fix_claimed`` key, exactly one of ``observed_fixed`` / ``reopened``
/ ``unobserved`` / ``invalidated``. The ledger itself (CC-01) treats an
*omitted* key as an implicit ``unobserved`` at ingest time -- but this
validator enforces a stricter bar on the inspector's own output, ahead of
ingestion: every prior key must be explicitly mentioned, so a worker
silently dropping a key is caught here rather than laundered into a
legitimate-looking ``unobserved`` downstream.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from harness_labs.core.convergence_contract import VERDICT_KINDS

__all__ = [
    "InspectorValidationError",
    "PriorKey",
    "validate_inspection_result",
]

PriorKey = tuple[str, str]


class InspectorValidationError(ValueError):
    """Raised when an inspection result fails the per-key verdict contract."""


def _coerce_key(raw: Any, *, owner: str) -> PriorKey:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or len(raw) != 2
    ):
        raise InspectorValidationError(f"{owner} key must be a [file, subject] pair, got {raw!r}")
    file_part, subject_part = raw
    if not isinstance(file_part, str) or not file_part:
        raise InspectorValidationError(f"{owner} key file must be a non-empty string, got {file_part!r}")
    if not isinstance(subject_part, str) or not subject_part:
        raise InspectorValidationError(
            f"{owner} key subject must be a non-empty string, got {subject_part!r}"
        )
    return (file_part, subject_part)


def _index_verdicts(result: Mapping[str, Any]) -> dict[PriorKey, Mapping[str, Any]]:
    raw_verdicts = result.get("verdicts")
    if not isinstance(raw_verdicts, Sequence) or isinstance(raw_verdicts, (str, bytes)):
        raise InspectorValidationError("inspection result must carry a 'verdicts' list")

    index: dict[PriorKey, Mapping[str, Any]] = {}
    for entry in raw_verdicts:
        if not isinstance(entry, Mapping):
            raise InspectorValidationError(f"each verdict entry must be an object, got {entry!r}")
        key = _coerce_key(entry.get("key"), owner="verdict")
        kind = entry.get("verdict")
        if kind not in VERDICT_KINDS:
            raise InspectorValidationError(
                f"verdict entry for {key!r} has an invalid verdict kind: {kind!r}"
            )
        if kind == "observed_fixed":
            if not entry.get("capture_cell"):
                raise InspectorValidationError(
                    f"observed_fixed verdict for {key!r} must cite a non-empty capture_cell"
                )
            if not entry.get("assertion"):
                raise InspectorValidationError(
                    f"observed_fixed verdict for {key!r} must cite the assertion evaluated"
                )
        if key in index:
            raise InspectorValidationError(f"duplicate verdict entry for key {key!r}")
        index[key] = entry
    return index


def validate_inspection_result(
    result: Mapping[str, Any],
    *,
    prior_keys: Iterable[Any],
) -> dict[PriorKey, Mapping[str, Any]]:
    """Validate ``result`` carries an explicit verdict for every prior key.

    ``prior_keys`` is every ``(file, subject)`` the task context marked
    ``open`` or ``fix_claimed`` before this audit ran. Each entry is coerced
    the same way verdict keys are, since the task context arrives as
    JSON-decoded ``[file, subject]`` lists, not Python tuples -- a caller
    passing that realistic shape must not crash with a raw ``TypeError``.
    Raises :class:`InspectorValidationError` naming every key the result is
    missing a verdict for (or any malformed verdict entry, including a
    malformed prior key); otherwise returns the validated
    ``key -> verdict entry`` mapping.
    """

    index = _index_verdicts(result)
    coerced_prior_keys = [_coerce_key(raw, owner="prior") for raw in prior_keys]
    missing = [key for key in coerced_prior_keys if key not in index]
    if missing:
        formatted = ", ".join(f"{file}:{subject}" for file, subject in missing)
        raise InspectorValidationError(
            f"inspection result is missing a mandatory verdict for prior key(s): {formatted}"
        )
    return index
