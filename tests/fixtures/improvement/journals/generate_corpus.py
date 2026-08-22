"""Regenerate the SI-02 run_forensics test corpus in ``corpus/``.

Every run directory below is written through
``harness_labs.core.audit.AuditJournal`` -- the repository's sole audit
writer -- so each journal is a genuine, hash-chain-valid artifact rather
than a hand-typed JSON blob shaped to whatever run_forensics.py currently
reads. ``run-si02-tampered-001`` is built the same way and then has one
event's payload text rewritten in place *without* recomputing that event's
``event_hash``, which is what makes its chain fail to verify.

Run with ``python3 -m tests.fixtures.improvement.journals.generate_corpus``
from the repository root after changing what a fixture run needs to
contain; do not hand-edit the generated ``events.jsonl``/``checkpoint.json``
files directly, since their hashes and chain links would no longer agree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal

CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"

ACTOR = AuditActor("si02-fixture-generator", "tool")


def _descriptor(run_id: str, evidence_classification: str) -> dict:
    return {
        "approved_plan": None,
        "created_at": "2026-08-01T00:00:00Z",
        "evidence_classification": evidence_classification,
        "objective": "SI-02 fixture run",
        "parent_correlation": None,
        "protocol": "harness-run-descriptor/1",
        "repository": {"base_commit": "0000000000000000000000000000000000000000"},
        "run_id": run_id,
        "run_kind": "feature_run",
    }


def _write_descriptor(run_dir: Path, run_id: str, evidence_classification: str) -> None:
    import json

    (run_dir / "descriptor.json").write_text(
        json.dumps(_descriptor(run_id, evidence_classification)), encoding="utf-8"
    )


def _finish(run_dir: Path, journal: AuditJournal, run_id: str, evidence_classification: str) -> None:
    journal.checkpoint("running", {})
    journal.release_liveness()
    _write_descriptor(run_dir, run_id, evidence_classification)
    (run_dir / ".audit.lock").unlink(missing_ok=True)
    for path in run_dir.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    run_dir.chmod(0o755)


def build_valid_001(run_dir: Path) -> None:
    run_id = "run-si02-valid-001"
    journal = AuditJournal(run_dir, run_id, actor=ACTOR, evidence_classification="production_lifecycle")

    journal.append(
        "controller_event",
        status="succeeded",
        payload={
            "controller_event": {
                "event_type": "retry.request",
                "reason": f"gate timeout at /Users/example/{run_id}/tests/test_thing.py:42",
            }
        },
        attempt_id="attempt-1",
    )

    journal.append(
        "deterministic_verification_completed",
        status="failed",
        payload={
            "node_id": "node-a",
            "classification": "product",
            "reason": f"assertion failed for {run_id} at 2026-08-01T00:00:05Z",
            "failure": {
                "classification": "product",
                "rule_id": "product-assertion",
                "evidence_excerpt": f"assertion failed for {run_id} at 2026-08-01T00:00:05Z",
            },
        },
        attempt_id="attempt-1",
    )

    journal.append(
        "node_status",
        status="blocked",
        payload={
            "node_id": "node-b",
            "reason": f"blocked waiting on operator decision for {run_id}",
        },
        attempt_id="attempt-2",
    )

    review_ledger = {
        "cycles": [
            {"cycle": 1, "distinct_findings": 2, "fix_keys": [], "review_attempt_id": "attempt-1"}
        ],
        "findings": {
            "finding-1": {
                "category": "correctness",
                "contract_violation": False,
                "cycles_seen": [1, 2],
                "evidence_refs": [],
                "file": "harness_labs/example.py",
                "fix_attempts": [{"cycle": 1, "outcome": "reopened"}],
                "fix_cost": "local",
                "key": "finding-1",
                "occurrences": 2,
                "outcome": "fixed",
                "protects": "runtime correctness",
                "reopened_count": 1,
                "requires_disposition": True,
                "scope_expanding": False,
                "score": 60,
                "severity": "major",
                "source_finding_ids": ["finding-1"],
                "statement": "Validation missing on retry path.",
                "subject": "widget validation",
            },
            "finding-2": {
                "category": "style",
                "contract_violation": False,
                "cycles_seen": [1],
                "evidence_refs": [],
                "file": "harness_labs/example.py",
                "fix_attempts": [],
                "fix_cost": "one-line",
                "key": "finding-2",
                "occurrences": 1,
                "outcome": "fixed",
                "protects": "readability",
                "reopened_count": 0,
                "requires_disposition": False,
                "scope_expanding": False,
                "score": 5,
                "severity": "info",
                "source_finding_ids": ["finding-2"],
                "statement": "Minor style issue, never reopened.",
                "subject": "unrelated style nit",
            },
        },
        "policy": {},
        "protocol": "review-ledger/1",
        "risk_tier": "mechanical",
    }
    review_artifact = journal.write_artifact("review-ledger", review_ledger)
    journal.append(
        "review_ledger_recorded",
        status="succeeded",
        payload={},
        artifacts=(review_artifact,),
    )

    abandoned_ledger = {
        "classification": "infrastructure_transient",
        "event": "abandoned",
        "failure_keys": ["gate:verification-timeout"],
        "lineage_id": "lineage-si02-valid",
        "node_id": "node-c",
        "protocol": "retry-budget-ledger/1",
        "reason": "exceeded retry ceiling",
    }
    abandoned_artifact = journal.write_artifact("retry-budget-ledger", abandoned_ledger)
    journal.append(
        "budget_ledger_recorded",
        status="succeeded",
        payload={},
        artifacts=(abandoned_artifact,),
    )

    extended_ledger = {
        "classification": "product",
        "event": "extended",
        "failure_keys": ["gate:flaky-assertion"],
        "lineage_id": "lineage-si02-valid",
        "node_id": "node-d",
        "protocol": "retry-budget-ledger/1",
        "reason": "operator granted extension",
    }
    extended_artifact = journal.write_artifact("retry-budget-ledger", extended_ledger)
    journal.append(
        "budget_ledger_recorded",
        status="succeeded",
        payload={},
        artifacts=(extended_artifact,),
    )

    _finish(run_dir, journal, run_id, "production_lifecycle")


def build_valid_002(run_dir: Path) -> None:
    """Also carries a foreign run id (``run-si02-valid-001``) and a bare
    (time-less) date in its retry reason text, so the corpus itself proves
    signature normalization strips run ids beyond the emitting run's own id
    and dates beyond the full-timestamp shape."""

    run_id = "run-si02-valid-002"
    journal = AuditJournal(run_dir, run_id, actor=ACTOR, evidence_classification="production_lifecycle")

    journal.append(
        "controller_event",
        status="succeeded",
        payload={
            "controller_event": {
                "event_type": "retry.request",
                "reason": (
                    f"gate timeout at /Users/example/{run_id}/tests/test_other.py:7 "
                    "following run-si02-valid-001 remediation on 2026-08-02"
                ),
            }
        },
        attempt_id="attempt-1",
    )

    extended_ledger = {
        "classification": "product",
        "event": "extended",
        "failure_keys": ["gate:flaky-assertion-2"],
        "lineage_id": "lineage-si02-valid",
        "node_id": "node-e",
        "protocol": "retry-budget-ledger/1",
        "reason": "operator granted extension",
    }
    extended_artifact = journal.write_artifact("retry-budget-ledger", extended_ledger)
    journal.append(
        "budget_ledger_recorded",
        status="succeeded",
        payload={},
        artifacts=(extended_artifact,),
    )

    _finish(run_dir, journal, run_id, "production_lifecycle")


def build_fabricated_001(run_dir: Path) -> None:
    run_id = "run-si02-fabricated-001"
    journal = AuditJournal(run_dir, run_id, actor=ACTOR, evidence_classification="fabricated_fixture")

    journal.append(
        "controller_event",
        status="succeeded",
        payload={
            "controller_event": {
                "event_type": "retry.request",
                "reason": "synthetic retry",
            }
        },
        attempt_id="attempt-1",
    )

    journal.append(
        "deterministic_verification_completed",
        status="failed",
        payload={"node_id": "node-a", "classification": "product", "reason": "synthetic failure"},
        attempt_id="attempt-1",
    )

    review_ledger = {
        "cycles": [{"cycle": 1, "distinct_findings": 1, "fix_keys": [], "review_attempt_id": "attempt-1"}],
        "findings": {
            "finding-1": {
                "category": "correctness",
                "contract_violation": False,
                "cycles_seen": [1, 2],
                "evidence_refs": [],
                "file": "harness_labs/example.py",
                "fix_attempts": [{"cycle": 1, "outcome": "reopened"}],
                "fix_cost": "local",
                "key": "finding-1",
                "occurrences": 2,
                "outcome": "fixed",
                "protects": "runtime correctness",
                "reopened_count": 1,
                "requires_disposition": True,
                "scope_expanding": False,
                "score": 60,
                "severity": "major",
                "source_finding_ids": ["finding-1"],
                "statement": "Synthetic reopened finding for a fabricated fixture run.",
                "subject": "synthetic finding",
            }
        },
        "policy": {},
        "protocol": "review-ledger/1",
        "risk_tier": "mechanical",
    }
    review_artifact = journal.write_artifact("review-ledger", review_ledger)
    journal.append(
        "review_ledger_recorded",
        status="succeeded",
        payload={},
        artifacts=(review_artifact,),
    )

    abandoned_ledger = {
        "classification": "infrastructure_transient",
        "event": "abandoned",
        "failure_keys": ["gate:synthetic"],
        "lineage_id": "lineage-si02-fabricated",
        "node_id": "node-c",
        "protocol": "retry-budget-ledger/1",
        "reason": "synthetic ceiling",
    }
    abandoned_artifact = journal.write_artifact("retry-budget-ledger", abandoned_ledger)
    journal.append(
        "budget_ledger_recorded",
        status="succeeded",
        payload={},
        artifacts=(abandoned_artifact,),
    )

    _finish(run_dir, journal, run_id, "fabricated_fixture")


def build_tampered_001(run_dir: Path) -> None:
    """Tampered by rewriting the last event's payload text after sealing
    *without* recomputing its event_hash, so the chain hash check --
    not a missing file or unreadable JSON -- is what refuses this run."""

    run_id = "run-si02-tampered-001"
    journal = AuditJournal(run_dir, run_id, actor=ACTOR, evidence_classification="production_lifecycle")

    journal.append(
        "controller_event",
        status="succeeded",
        payload={
            "controller_event": {
                "event_type": "retry.request",
                "reason": "gate timeout",
            }
        },
        attempt_id="attempt-1",
    )

    journal.append(
        "deterministic_verification_completed",
        status="failed",
        payload={"node_id": "node-a", "classification": "product", "reason": "assertion failed before tampering"},
        attempt_id="attempt-1",
    )

    _finish(run_dir, journal, run_id, "production_lifecycle")

    events_path = run_dir / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace(
        "assertion failed before tampering",
        "TAMPERED: this text was altered after sealing",
    )
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    for name, builder in (
        ("run-si02-valid-001", build_valid_001),
        ("run-si02-valid-002", build_valid_002),
        ("run-si02-fabricated-001", build_fabricated_001),
        ("run-si02-tampered-001", build_tampered_001),
    ):
        run_dir = CORPUS_ROOT / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        builder(run_dir)
        print(f"built {name}")


if __name__ == "__main__":
    main()
