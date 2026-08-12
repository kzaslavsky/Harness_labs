"""Controller-owned, ledger-backed review/fix orchestration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from .attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
from .controller_evidence import EvidenceCatalog
from .controller_results import validate_semantic_result
from .git_transaction import paths_outside_scope


REVIEW_LEDGER_PROTOCOL = "review-ledger/1"
REVIEW_FIX_RESULT_PROTOCOL = "review-fix-result/1"
_SEVERITY_SCORE = {"critical": 95, "major": 85, "minor": 60, "info": 20}
_KEY_TEXT = re.compile(r"[^a-z0-9._/-]+")


class ReviewFixError(RuntimeError):
    """Raised when the review/fix protocol cannot safely continue."""


class _RecoverableFixError(ReviewFixError):
    """A fix worker failed mechanically while the frozen work remains valid."""


class ReviewFixExecutorFactory(Protocol):
    """Construct an executor for one immutable review/fix stage attempt."""

    def __call__(self, stage: str, attempt: TaskAttempt) -> Executor:
        """Return the stage executor."""


@dataclass(frozen=True)
class ReviewFixPolicy:
    """Every anti-divergence mechanism is an explicit, serializable switch."""

    enabled: bool = True
    ledger_enabled: bool = True
    deduplication_enabled: bool = True
    reraise_guard_enabled: bool = True
    citation_guard_enabled: bool = True
    scope_expansion_guard_enabled: bool = True
    targeted_verification_enabled: bool = True
    regression_review_enabled: bool = True
    cycle_limit_enabled: bool = True
    risk_tiering_enabled: bool = True
    marginal_yield_stop_enabled: bool = True
    no_progress_stop_enabled: bool = True
    technical_debt_sink_enabled: bool = False
    allow_required_technical_debt: bool = False
    fix_score_threshold: int = 80
    note_score_threshold: int = 50
    mechanical_cycle_limit: int = 3
    sensitive_cycle_limit: int = 5
    minimum_yield: float = 0.10
    low_yield_cycles: int = 2

    def __post_init__(self) -> None:
        if not 0 <= self.note_score_threshold <= self.fix_score_threshold <= 100:
            raise ValueError(
                "review score thresholds must satisfy 0 <= note <= fix <= 100"
            )
        if self.mechanical_cycle_limit < 1 or self.sensitive_cycle_limit < 1:
            raise ValueError("review cycle limits must be positive")
        if not 0 <= self.minimum_yield <= 1:
            raise ValueError("minimum_yield must be between zero and one")
        if self.low_yield_cycles < 1:
            raise ValueError("low_yield_cycles must be positive")


@dataclass(frozen=True)
class ReviewFixResult:
    status: str
    reason: str
    cycles: int
    risk_tier: str
    ledger_ref: str
    open_finding_keys: tuple[str, ...]
    technical_debt_keys: tuple[str, ...]
    transferred_findings: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": REVIEW_FIX_RESULT_PROTOCOL,
            "status": self.status,
            "reason": self.reason,
            "cycles": self.cycles,
            "risk_tier": self.risk_tier,
            "ledger_ref": self.ledger_ref,
            "open_finding_keys": list(self.open_finding_keys),
            "technical_debt_keys": list(self.technical_debt_keys),
            "transferred_findings": [
                dict(item) for item in self.transferred_findings
            ],
        }


class ReviewLedger:
    """Authoritative finding identity, disposition, and cycle history."""

    def __init__(self, policy: ReviewFixPolicy, risk_tier: str) -> None:
        self.policy = policy
        self.risk_tier = risk_tier
        self.findings: dict[str, dict[str, Any]] = {}
        self.cycles: list[dict[str, Any]] = []
        self.discovery_frozen = False

    def seed_transferred(
        self, findings: tuple[Mapping[str, Any], ...]
    ) -> None:
        """Reopen inherited obligations without changing their stable identity."""

        for finding in findings:
            key = str(finding.get("key", ""))
            if not key or key in self.findings:
                raise ReviewFixError(
                    f"transferred finding has an empty or duplicate key: {key!r}"
                )
            record = dict(finding)
            source = str(record.get("transferred_to", ""))
            record["outcome"] = "open"
            record["outcome_reason"] = (
                f"inherited from {source}" if source else "inherited"
            )
            record["transferred_to"] = ""
            record["transfer_eligible"] = bool(
                record.get("transfer_eligible", False) and not source
            )
            record.setdefault("origin_node", "")
            record.setdefault("cycles_seen", [])
            record.setdefault("occurrences", 1)
            record.setdefault("source_finding_ids", [key])
            record.setdefault("evidence_refs", [])
            record.setdefault("fix_attempts", [])
            record.setdefault("reopened_count", 0)
            record.setdefault("required_paths", [])
            self.findings[key] = record

    def seed_retained_transfers(
        self, findings: tuple[Mapping[str, Any], ...]
    ) -> None:
        """Restore obligations already transferred by a replaced review loop."""

        for finding in findings:
            key = str(finding.get("key", ""))
            target = str(finding.get("transferred_to", ""))
            if not key or not target or key in self.findings:
                raise ReviewFixError(
                    "retained transfer must have a unique key and downstream owner"
                )
            record = dict(finding)
            record["outcome"] = "transferred"
            record.setdefault(
                "outcome_reason",
                f"retained by recovery for downstream owner {target}",
            )
            record.setdefault("origin_node", "")
            record.setdefault("cycles_seen", [])
            record.setdefault("occurrences", 1)
            record.setdefault("source_finding_ids", [key])
            record.setdefault("evidence_refs", [])
            record.setdefault("fix_attempts", [])
            record.setdefault("reopened_count", 0)
            record.setdefault("required_paths", [])
            self.findings[key] = record

    def freeze_discovery(self) -> None:
        self.discovery_frozen = True

    def transfer_scope_expanding(
        self,
        targets: Mapping[str, str],
        *,
        origin_node: str,
        current_paths: tuple[str, ...] = (),
    ) -> list[str]:
        """Move eligible findings to their uniquely pre-bound downstream owner."""

        transferred: list[str] = []
        for key, record in self.findings.items():
            if (
                record["outcome"] != "open"
                or not record["scope_expanding"]
                or not record.get("transfer_eligible", True)
            ):
                continue
            required_paths = record.get("required_paths", ())
            downstream_paths = [
                str(path) for path in required_paths if str(path) not in current_paths
            ]
            resolved = [_target_for_path(path, targets) for path in downstream_paths]
            owners = set(resolved)
            if not downstream_paths or None in owners or len(owners) != 1:
                continue
            target = next(iter(owners))
            record["required_paths"] = downstream_paths
            record["outcome"] = "transferred"
            record["outcome_reason"] = f"transferred to downstream owner {target}"
            record["origin_node"] = origin_node
            record["transferred_to"] = target
            transferred.append(key)
        return sorted(transferred)

    def transferred(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            dict(item)
            for _, item in sorted(self.findings.items())
            if item["outcome"] == "transferred"
        )

    def ingest(
        self,
        findings: tuple[Mapping[str, Any], ...],
        *,
        cycle: int,
    ) -> tuple[list[str], dict[str, int]]:
        current: set[str] = set()
        duplicates = 0
        ledger_collapses = 0
        deferred_findings = 0
        for ordinal, finding in enumerate(findings, start=1):
            key = _finding_key(finding)
            if not self.policy.ledger_enabled:
                key = f"cycle-{cycle}/{ordinal}/{key}"
            elif key in current and not self.policy.deduplication_enabled:
                key = f"{key}#cycle-{cycle}-occurrence-{ordinal}"
            if key in current and self.policy.deduplication_enabled:
                duplicates += 1
                self._merge_occurrence(self.findings[key], finding, cycle)
                continue
            current.add(key)
            existing = self.findings.get(key)
            if existing is None:
                record = self._new_record(key, finding, cycle)
                if cycle > 1 or self.discovery_frozen:
                    record["outcome"] = "deferred"
                    record["outcome_reason"] = (
                        "discovery frozen after the first review"
                    )
                    deferred_findings += 1
                self.findings[key] = record
                continue
            self._merge_occurrence(existing, finding, cycle)
            if existing["outcome"] == "transferred":
                ledger_collapses += 1
                continue
            if (
                self.policy.reraise_guard_enabled
                and existing["outcome"] in {"fixed", "note", "scope_screened", "debt"}
                and not finding.get("new_evidence")
            ):
                ledger_collapses += 1
                continue
            if existing["outcome"] != "pending_review":
                existing["outcome"] = "open"

        fixed_by_absence = 0
        if self.policy.regression_review_enabled:
            for key, record in self.findings.items():
                if record["outcome"] == "pending_review":
                    if key in current:
                        record["outcome"] = "open"
                        record["reopened_count"] += 1
                    else:
                        record["outcome"] = "fixed"
                        fixed_by_absence += 1

        for record in self.findings.values():
            if record["outcome"] != "open":
                continue
            if (
                self.policy.scope_expansion_guard_enabled
                and record["scope_expanding"]
                and not record["contract_violation"]
                and not record["requires_disposition"]
            ):
                record["outcome"] = "scope_screened"
                continue
            if (
                self.policy.citation_guard_enabled
                and record["score"] >= self.policy.fix_score_threshold
                and not record["protects"]
                and not record["contract_violation"]
                and not record["requires_disposition"]
            ):
                record["outcome"] = "note"
                record["outcome_reason"] = "missing normative protects citation"
                continue
            if (
                record["score"] < self.policy.fix_score_threshold
                and not record["contract_violation"]
                and not record["requires_disposition"]
            ):
                record["outcome"] = "note"

        fix_keys = sorted(
            key for key in current if self.findings[key]["outcome"] == "open"
        )
        return fix_keys, {
            "within_cycle_duplicates": duplicates,
            "ledger_collapses": ledger_collapses,
            "deferred_findings": deferred_findings,
            "fixed_by_re_review": fixed_by_absence,
            "distinct_findings": len(current),
        }

    def mark_fix_attempt(
        self,
        requested: list[str],
        addressed: list[str],
        cycle: int,
    ) -> None:
        unknown = sorted(set(addressed) - set(requested))
        if unknown:
            raise ReviewFixError(
                "fixer claimed findings outside its fix list: " + ", ".join(unknown)
            )
        for key in requested:
            record = self.findings[key]
            record["fix_attempts"].append(
                {"cycle": cycle, "addressed": key in addressed}
            )

    def mark_verified(self, addressed: list[str], verified: list[str]) -> None:
        for key in addressed:
            if key in verified:
                self.findings[key]["outcome"] = (
                    "pending_review"
                    if self.policy.regression_review_enabled
                    else "fixed"
                )
            else:
                self.findings[key]["outcome"] = "open"

    def open_required(self) -> list[str]:
        return sorted(
            key
            for key, item in self.findings.items()
            if item["outcome"] in {"open", "pending_review"}
            and (item["requires_disposition"] or item["contract_violation"])
        )

    def open_all(self) -> list[str]:
        return sorted(
            key
            for key, item in self.findings.items()
            if item["outcome"] in {"open", "pending_review"}
        )

    def apply_debt_sink(self) -> None:
        for item in self.findings.values():
            if item["outcome"] not in {"open", "pending_review"}:
                continue
            required = item["requires_disposition"] or item["contract_violation"]
            if required and not self.policy.allow_required_technical_debt:
                continue
            item["outcome"] = "debt"

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": REVIEW_LEDGER_PROTOCOL,
            "policy": asdict(self.policy),
            "risk_tier": self.risk_tier,
            "findings": {
                key: dict(value) for key, value in sorted(self.findings.items())
            },
            "cycles": list(self.cycles),
        }

    def _new_record(
        self,
        key: str,
        finding: Mapping[str, Any],
        cycle: int,
    ) -> dict[str, Any]:
        severity = str(finding.get("severity", "major"))
        score = finding.get("score", _SEVERITY_SCORE.get(severity, 0))
        if not isinstance(score, int) or not 0 <= score <= 100:
            raise ReviewFixError(f"finding {key} has an invalid score")
        return {
            "key": key,
            "file": str(finding.get("file", "")),
            "subject": str(finding.get("subject", finding.get("statement", ""))),
            "statement": str(finding.get("statement", "")),
            "category": str(finding.get("category", "review")),
            "severity": severity,
            "score": score,
            "fix_cost": str(finding.get("fix_cost", "local")),
            "protects": str(finding.get("protects", "")),
            "requires_disposition": bool(finding.get("requires_disposition", False)),
            "contract_violation": bool(finding.get("contract_violation", False)),
            "scope_expanding": bool(
                finding.get("scope_expanding", False)
                or finding.get("fix_cost") == "surface-growing"
            ),
            "outcome": "open",
            "outcome_reason": "",
            "cycles_seen": [cycle],
            "occurrences": 1,
            "source_finding_ids": [str(finding.get("id", key))],
            "evidence_refs": list(finding.get("evidence_refs", ())),
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "",
            "transferred_to": "",
            "transfer_eligible": True,
            "required_paths": list(finding.get("required_paths", ())),
        }

    @staticmethod
    def _merge_occurrence(
        record: dict[str, Any],
        finding: Mapping[str, Any],
        cycle: int,
    ) -> None:
        record["occurrences"] += 1
        if cycle not in record["cycles_seen"]:
            record["cycles_seen"].append(cycle)
        source_id = str(finding.get("id", record["key"]))
        if source_id not in record["source_finding_ids"]:
            record["source_finding_ids"].append(source_id)
        for ref in finding.get("evidence_refs", ()):
            if ref not in record["evidence_refs"]:
                record["evidence_refs"].append(ref)
        for path in finding.get("required_paths", ()):
            if path not in record["required_paths"]:
                record["required_paths"].append(path)
        incoming_score = finding.get(
            "score",
            _SEVERITY_SCORE.get(str(finding.get("severity", "major")), 0),
        )
        if isinstance(incoming_score, int):
            record["score"] = max(record["score"], incoming_score)


class ReviewFixLoop:
    """Run independent review, bounded repair, verification, and re-review."""

    def __init__(
        self,
        *,
        run_id: str,
        objective: str,
        acceptance_criteria: tuple[Mapping[str, Any], ...],
        allowed_paths: tuple[str, ...],
        changed_paths: tuple[str, ...],
        executor_factory: ReviewFixExecutorFactory,
        evidence: EvidenceCatalog,
        audit: AuditJournal,
        policy: ReviewFixPolicy = ReviewFixPolicy(),
        inherited_findings: tuple[Mapping[str, Any], ...] = (),
        retained_transfers: tuple[Mapping[str, Any], ...] = (),
        finding_transfer_targets: Mapping[str, str] | None = None,
        origin_node_id: str = "",
        inherited_ledger_frozen: bool = False,
    ) -> None:
        self.run_id = run_id
        self.objective = objective
        self.acceptance_criteria = acceptance_criteria
        self.allowed_paths = allowed_paths
        self.changed_paths = changed_paths
        self.executor_factory = executor_factory
        self.evidence = evidence
        self.audit = audit
        self.policy = policy
        self.inherited_findings = inherited_findings
        self.retained_transfers = retained_transfers
        self.finding_transfer_targets = dict(finding_transfer_targets or {})
        self.origin_node_id = origin_node_id or run_id
        self.inherited_ledger_frozen = inherited_ledger_frozen
        self.runner = AttemptRunner()

    def run(self) -> ReviewFixResult:
        risk_tier = _risk_tier(self.changed_paths, self.policy)
        ledger = ReviewLedger(self.policy, risk_tier)
        ledger.seed_transferred(self.inherited_findings)
        ledger.seed_retained_transfers(self.retained_transfers)
        if self.inherited_ledger_frozen:
            ledger.freeze_discovery()
        if not self.policy.enabled:
            return self._finish(ledger, "succeeded", "review-fix loop disabled", 0)
        cycle_limit = (
            self.policy.sensitive_cycle_limit
            if risk_tier == "sensitive"
            else self.policy.mechanical_cycle_limit
        )
        low_yield_streak = 0
        cycle = 0
        try:
            while True:
                cycle += 1
                review = self._execute("review", cycle, ledger)
                semantic = self._semantic(review, "review-fix-review/1")
                findings = tuple(
                    {
                        **dict(finding),
                        "scope_expanding": bool(
                            finding.get("scope_expanding", False)
                            or paths_outside_scope(
                                finding.get("required_paths", ()),
                                self.allowed_paths,
                            )
                        ),
                    }
                    for finding in semantic.findings
                )
                fix_keys, counts = ledger.ingest(findings, cycle=cycle)
                transferred = ledger.transfer_scope_expanding(
                    self.finding_transfer_targets,
                    origin_node=self.origin_node_id,
                    current_paths=self.allowed_paths,
                )
                fix_keys = sorted(
                    key
                    for key in set(fix_keys) | set(ledger.open_all())
                    if ledger.findings[key]["outcome"] == "open"
                )
                cycle_entry: dict[str, Any] = {
                    "cycle": cycle,
                    "review_attempt_id": review.attempt_id,
                    **counts,
                    "fix_keys": list(fix_keys),
                    "transferred_finding_keys": transferred,
                }
                ledger.cycles.append(cycle_entry)
                self._persist(ledger, "review_completed", cycle_entry)

                if not fix_keys:
                    if ledger.open_required():
                        return self._finish(
                            ledger,
                            "blocked",
                            "required findings remain open",
                            cycle,
                        )
                    return self._finish(ledger, "succeeded", "review cleared", cycle)

                if self.policy.cycle_limit_enabled and cycle >= cycle_limit:
                    return self._limit_exit(ledger, cycle, "cycle limit reached")

                try:
                    fix = self._execute("fix", cycle, ledger, fix_keys=fix_keys)
                except _RecoverableFixError as exc:
                    self.audit.append(
                        "review_fix_recovery_triggered",
                        status="recovering",
                        payload={
                            "cycle": cycle,
                            "fix_keys": fix_keys,
                            "reason": str(exc),
                            "recovery_attempt": 1,
                        },
                        actor=AuditActor("review-fix-controller", "controller"),
                    )
                    fix = self._execute(
                        "fix",
                        cycle,
                        ledger,
                        fix_keys=fix_keys,
                        recovery_attempt=1,
                        recovery_reason=str(exc),
                    )
                fix_semantic = self._semantic(fix, "review-fix-fix/1")
                addressed = _detail_keys(
                    fix_semantic.details,
                    "addressed_finding_keys",
                )
                ledger.mark_fix_attempt(fix_keys, addressed, cycle)
                if self.policy.no_progress_stop_enabled and not addressed:
                    self._persist(
                        ledger,
                        "review_fix_no_progress",
                        {"cycle": cycle, "fix_keys": fix_keys},
                    )
                    return self._limit_exit(ledger, cycle, "fixer made no progress")

                verified = addressed
                verify_attempt_id = None
                if self.policy.targeted_verification_enabled:
                    verification = self._execute(
                        "verify",
                        cycle,
                        ledger,
                        fix_keys=addressed,
                    )
                    verify_attempt_id = verification.attempt_id
                    verify_semantic = self._semantic(
                        verification,
                        "review-fix-verify/1",
                    )
                    verified = _detail_keys(
                        verify_semantic.details,
                        "verified_finding_keys",
                    )
                ledger.mark_verified(addressed, verified)
                yield_value = len(addressed) / max(counts["distinct_findings"], 1)
                cycle_entry.update(
                    {
                        "fix_attempt_id": fix.attempt_id,
                        "verify_attempt_id": verify_attempt_id,
                        "addressed_finding_keys": addressed,
                        "verified_finding_keys": verified,
                        "yield": yield_value,
                    }
                )
                self._persist(ledger, "review_fix_cycle_completed", cycle_entry)

                if not self.policy.regression_review_enabled:
                    if ledger.open_required():
                        return self._finish(
                            ledger,
                            "blocked",
                            "required findings remain open",
                            cycle,
                        )
                    return self._finish(
                        ledger,
                        "succeeded",
                        "verified fixes accepted without regression re-review",
                        cycle,
                    )
                low_yield_streak = (
                    low_yield_streak + 1
                    if yield_value < self.policy.minimum_yield
                    else 0
                )
                if (
                    self.policy.marginal_yield_stop_enabled
                    and low_yield_streak >= self.policy.low_yield_cycles
                ):
                    return self._limit_exit(
                        ledger,
                        cycle,
                        "marginal yield stop",
                    )
        except InterruptedError as exc:
            self.audit.append(
                "review_fix_failed",
                status="interrupted",
                payload={"error": str(exc), "cycle": cycle},
                actor=AuditActor("review-fix-controller", "controller"),
            )
            return self._finish(
                ledger,
                "interrupted",
                str(exc) or "review-fix interrupted",
                cycle,
            )
        except Exception as exc:
            self.audit.append(
                "review_fix_failed",
                status="failed",
                payload={"error": str(exc), "cycle": cycle},
                actor=AuditActor("review-fix-controller", "controller"),
            )
            return self._finish(ledger, "failed", str(exc), cycle)

    def _execute(
        self,
        stage: str,
        cycle: int,
        ledger: ReviewLedger,
        *,
        fix_keys: list[str] | None = None,
        recovery_attempt: int | None = None,
        recovery_reason: str = "",
    ) -> TaskResult:
        suffix = f"-recovery-{recovery_attempt}" if recovery_attempt else ""
        attempt_id = f"{self.run_id}/review-fix/c{cycle}/{stage}{suffix}"
        context = {
            "protocol": "review-fix-context/1",
            "stage": stage,
            "cycle": cycle,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_paths": list(self.allowed_paths),
            "changed_paths": list(self.changed_paths),
            "fix_finding_keys": list(fix_keys or ()),
            "ledger": ledger.as_dict(),
            "output_contract": _stage_output_contract(stage),
            "regression_focus": (
                "Check only whether findings from the first review remain after "
                "their fixes. Do not discover or authorize new work."
                if stage == "review"
                and (cycle > 1 or self.inherited_ledger_frozen)
                and self.policy.regression_review_enabled
                else ""
            ),
            "recovery": (
                {
                    "attempt": recovery_attempt,
                    "reason": recovery_reason,
                    "instruction": (
                        "Use a changed implementation method for the same frozen "
                        "finding keys; do not expand scope or discovery."
                    ),
                }
                if recovery_attempt
                else None
            ),
        }
        attempt = TaskAttempt(
            attempt_id=attempt_id,
            task_ref=f"review-fix:{stage}",
            context_ref=_digest(context),
            grant_ref=("grant:repo.write" if stage == "fix" else "grant:repo.read"),
            parent_attempt_id=f"{self.run_id}/integration-owner",
            context=json.dumps(context, sort_keys=True),
        )
        result = self.runner.run(
            attempt,
            self.executor_factory(stage, attempt),
        )
        if result.status != "succeeded":
            error = str(result.payload.get("error", ""))
            if (
                stage == "fix"
                and recovery_attempt is None
                and error
                == "writable worker completed without changing the repository"
            ):
                raise _RecoverableFixError(
                    f"{stage} attempt ended with status {result.status}: "
                    f"{result.payload}"
                )
            raise ReviewFixError(
                f"{stage} attempt ended with status {result.status}: {result.payload}"
            )
        receipt = self.evidence.add(
            kind=f"review-fix-{stage}-result",
            content={
                "attempt_id": result.attempt_id,
                "status": result.status,
                "payload": dict(result.payload),
                "evidence": list(result.evidence),
            },
            media_type="application/json",
            producer_task_id=attempt.attempt_id,
        )
        self.audit.append(
            "review_fix_stage_completed",
            status="succeeded",
            payload={
                "stage": stage,
                "cycle": cycle,
                "result_ref": receipt.ref,
            },
            actor=AuditActor(
                attempt.attempt_id,
                f"review_fix_{stage}",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
        )
        return result

    @staticmethod
    def _semantic(result: TaskResult, schema: str):
        return validate_semantic_result(result, expected_details_schema=schema)

    def _limit_exit(
        self,
        ledger: ReviewLedger,
        cycle: int,
        reason: str,
    ) -> ReviewFixResult:
        if self.policy.technical_debt_sink_enabled:
            ledger.apply_debt_sink()
        if ledger.open_all():
            return self._finish(ledger, "blocked", reason, cycle)
        return self._finish(
            ledger, "succeeded", f"{reason}; remaining items recorded as debt", cycle
        )

    def _persist(
        self,
        ledger: ReviewLedger,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str:
        artifact = self.evidence.add(
            kind="review-ledger",
            content=ledger.as_dict(),
            media_type="application/json",
            producer_task_id="review-fix-controller",
        )
        self.audit.append(
            event_type,
            status=str(payload.get("status", "succeeded")),
            payload={**dict(payload), "ledger_ref": artifact.ref},
            actor=AuditActor("review-fix-controller", "controller"),
        )
        self.audit.merge_checkpoint(
            updates={
                "review_fix": {
                    "ledger_ref": artifact.ref,
                    "risk_tier": ledger.risk_tier,
                    "cycles": len(ledger.cycles),
                    "open_finding_keys": ledger.open_all(),
                }
            }
        )
        return artifact.ref

    def _finish(
        self,
        ledger: ReviewLedger,
        status: str,
        reason: str,
        cycles: int,
    ) -> ReviewFixResult:
        ledger_ref = self._persist(
            ledger,
            "review_fix_completed",
            {"status": status, "reason": reason, "cycles": cycles},
        )
        debt = tuple(
            sorted(
                key
                for key, item in ledger.findings.items()
                if item["outcome"] == "debt"
            )
        )
        return ReviewFixResult(
            status,
            reason,
            cycles,
            ledger.risk_tier,
            ledger_ref,
            tuple(ledger.open_all()),
            debt,
            ledger.transferred(),
        )


def _finding_key(finding: Mapping[str, Any]) -> str:
    path = str(finding.get("file", "")).strip().lower() or "<repository>"
    subject = str(finding.get("subject", finding.get("statement", ""))).strip().lower()
    if not subject:
        subject = str(finding.get("id", "finding")).lower()
    normalized = _KEY_TEXT.sub("-", subject).strip("-")[:120] or "finding"
    return f"{path}:{normalized}"


def _target_for_path(path: str, targets: Mapping[str, str]) -> str | None:
    matches = []
    for grant, target in targets.items():
        normalized = grant.rstrip("/")
        if path == normalized or (grant.endswith("/") and path.startswith(grant)):
            matches.append((len(normalized), target))
    if not matches:
        return None
    longest = max(length for length, _ in matches)
    owners = {target for length, target in matches if length == longest}
    return next(iter(owners)) if len(owners) == 1 else None


def _detail_keys(details: Mapping[str, Any], name: str) -> list[str]:
    value = details.get(name)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ReviewFixError(f"{name} must be a list of finding keys")
    return list(dict.fromkeys(value))


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "context:sha256:" + hashlib.sha256(raw).hexdigest()


def _risk_tier(paths: tuple[str, ...], policy: ReviewFixPolicy) -> str:
    if not policy.risk_tiering_enabled:
        return "sensitive"
    sensitive_markers = (
        "auth",
        "security",
        "schema",
        "migration",
        "store",
        "sql",
        "route",
        "template",
        "static/",
        ".js",
        ".ts",
        ".tsx",
        ".html",
    )
    if not paths:
        return "sensitive"
    return (
        "sensitive"
        if any(marker in path.lower() for path in paths for marker in sensitive_markers)
        else "mechanical"
    )


def _stage_output_contract(stage: str) -> Mapping[str, Any]:
    if stage == "review":
        return {
            "details_schema": "review-fix-review/1",
            "finding_fields": [
                "file",
                "subject",
                "score",
                "fix_cost",
                "protects",
                "scope_expanding",
                "contract_violation",
                "new_evidence",
                "required_paths",
            ],
        }
    key = "addressed_finding_keys" if stage == "fix" else "verified_finding_keys"
    return {
        "details_schema": f"review-fix-{stage}/1",
        "required_details": {key: "list[string]"},
    }


__all__ = [
    "REVIEW_FIX_RESULT_PROTOCOL",
    "REVIEW_LEDGER_PROTOCOL",
    "ReviewFixError",
    "ReviewFixExecutorFactory",
    "ReviewFixLoop",
    "ReviewFixPolicy",
    "ReviewFixResult",
    "ReviewLedger",
]
