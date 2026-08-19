"""Closed vocabularies for the convergence campaign contract (CC-01).

Core-layer module: it imports nothing from ``harness_labs.plangraph``, so
that plangraph-layer consumers (``harness_labs.plangraph.convergence_ledger``,
and later the CC-03 measurer) import these vocabularies from here rather
than the reverse — ``tests/test_import_boundaries.py`` enforces that no
``core``-layer module reaches into ``plangraph``.
"""

from __future__ import annotations

VERDICT_KINDS = frozenset({"observed_fixed", "reopened", "unobserved", "invalidated"})
"""Every audit returns exactly one of these for each prior open/fix_claimed
key (``contracts-verdicts``). A key the inspector does not mention is
``unobserved`` by default; only ``observed_fixed`` closes a key."""

RULING_DISPOSITIONS = frozenset({"waive", "require_repair", "amend_criterion"})
"""The closed set of human rule-step dispositions (``contracts-rulings``).
Only ``waive`` enters the exclusion set; ``require_repair`` keeps the key
open with the ruling text as its acceptance statement; ``amend_criterion``
closes the key through a criteria-amendment transaction."""

CAPTURE_CELL_STATUSES = frozenset({"ok", "unreachable", "unstable"})
"""Per-capture-cell coverage status. A verdict citing a cell recorded
``unstable`` cannot write ``finding_fixed`` (``contracts-verdicts``)."""
