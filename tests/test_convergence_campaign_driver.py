"""Tests for the convergence campaign driver (CC-04).

Covers every AC-CC04-* acceptance criterion plus the ``tests-driver``
checklist: termination predicate (coverage, recall, amendment-ratio
acknowledgment); harvest on both block paths; base-adoption; round bound
with the audit outside it; stall escalation and ``regression_suspect``
ordering; resume from every step; predecessor refusal; no silent approval;
byte-identity approval precondition; audit-on-seal.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.plangraph.convergence_ledger import ConvergenceLedger
from harness_labs.plangraph.plan_graph import FeatureRunOutcome, PlanGraph, register_plan_graph

from scripts.run_convergence_campaign import (
    ApprovalPacket,
    ByteIdentityViolation,
    ConvergenceCampaignDriver,
    ConvergenceCampaignDriverError,
    HarvestedFinding,
    PredecessorResumableError,
    RepairRoundBoundExceeded,
    RepairRoundBudget,
    ResumeDirective,
    SanitizerFailure,
    StallEscalation,
    TargetAmendedWithoutScopeError,
    UnacknowledgedWarningsError,
    base_adoption_decision,
    check_criteria_byte_identity,
    check_objective_in_plan_text,
    extract_plan_section,
    guard_before_plan,
    harvest_unrouted_findings,
    issue_approval,
    join_regression_node_id,
    main,
    predecessor_is_resumable,
    render_approval_packet,
    render_findings_owners_paths_table,
    resume_directive_from_escalation,
    sanitize_before_journaling,
    sibling_overlap_warnings_from_gate_evidence,
    tag_regression_suspects,
    unacknowledged_warnings,
    validate_round_grants,
    evaluate_success_termination,
)


def _finding(file: str, subject: str, **overrides) -> dict:
    finding = {"file": file, "subject": subject, "required_paths": [file], "confidence": "C"}
    finding.update(overrides)
    return finding


def _audit(digest: str, *, findings=(), verdicts=(), capture_coverage=None) -> dict:
    return {
        "digest": digest,
        "findings": list(findings),
        "verdicts": list(verdicts),
        "confirmed_good": [],
        "capture_coverage": capture_coverage or {},
    }


# A fake capture command that behaves like the shipped
# ``scripts/ui_fidelity_capture.py`` CLI: it takes ``--out`` (and other
# capture-flavored flags it ignores) the way the real CLI does, writes its
# result to ``<--out>/receipt.json``, and -- on the success path -- prints
# nothing to stdout at all. ``--noisy-stdout`` simulates a driver that logs
# progress to stdout without that output being the audit result, and
# ``--skip-receipt`` simulates a run that exited zero without ever writing
# a receipt.
_FAKE_CAPTURE_SCRIPT = """
import argparse, pathlib, sys

parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True)
parser.add_argument("--app-dir", default=None)
parser.add_argument("--matrix", default=None)
parser.add_argument("--python", default=None)
parser.add_argument("--payload", required=True)
parser.add_argument("--noisy-stdout", action="store_true")
parser.add_argument("--skip-receipt", action="store_true")
args = parser.parse_args()

if args.noisy_stdout:
    print("ui_fidelity_capture: launching browser driver...")

out_dir = pathlib.Path(args.out)
out_dir.mkdir(parents=True, exist_ok=True)
if not args.skip_receipt:
    (out_dir / "receipt.json").write_text(args.payload, encoding="utf-8")
sys.exit(0)
"""


def _fake_capture_argv(
    out_dir: Path, payload: dict, *, noisy_stdout: bool = False, skip_receipt: bool = False,
) -> list:
    argv = [
        sys.executable, "-c", _FAKE_CAPTURE_SCRIPT,
        "--out", str(out_dir), "--payload", json.dumps(payload),
    ]
    if noisy_stdout:
        argv.append("--noisy-stdout")
    if skip_receipt:
        argv.append("--skip-receipt")
    return argv


def _uppercasing_pre_journal_sanitizer(text: str) -> str:
    """A transforming (non-identity) ``pre_journal_sanitizer`` hook, used to
    prove a resolved hook actually ran rather than merely resolving without
    being invoked: ``.upper()`` leaves JSON's structural characters and
    lowercase ``true``/``false``/``null`` keywords untouched, so it is safe
    to run over a JSON receipt that carries no booleans or nulls, while
    still visibly changing every letter it does carry."""

    return text.upper()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RepoFixture(unittest.TestCase):
    """A real git repository with the repository-identity artifact admission
    requires, matching ``tests/test_plan_approval.py``'s setup."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "cc04-tests@example.com")
        self._git("config", "user.name", "CC-04 Driver Tests")
        (self.repository / ".harness").mkdir()
        self._write_json(
            self.repository / ".harness" / "repository.json",
            {
                "protocol": "harness-repository-identity/1",
                "repository_id": "cc04-driver-test-repository",
            },
        )
        self.campaign_root = self.root / "campaign"

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True, capture_output=True, check=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def _capture_argv(self, payload: dict) -> tuple[list, Path]:
        """A fake capture command matching the shipped
        ``scripts/ui_fidelity_capture.py`` contract: it writes its result to
        ``<out_dir>/receipt.json`` and prints nothing to stdout on success.

        Written to a real script file (rather than ``python -c``) so the
        resulting argv contains no ``-``-prefixed tokens: the CLI's own
        ``--capture-argv`` (``nargs="+"``) would otherwise misinterpret a
        leading ``-c`` as terminating the argument list.
        """

        out_dir = Path(tempfile.mkdtemp(dir=self.root))
        script_path = Path(tempfile.mkstemp(dir=self.root, suffix=".py")[1])
        script_path.write_text(
            "import json, pathlib, sys\n"
            "out = pathlib.Path(sys.argv[1])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / 'receipt.json').write_text(sys.argv[2])\n",
            encoding="utf-8",
        )
        argv = [sys.executable, str(script_path), str(out_dir), json.dumps(payload)]
        return argv, out_dir

    def driver(self, **overrides) -> ConvergenceCampaignDriver:
        arguments = {
            "campaign_root": self.campaign_root,
            "campaign_id": "camp-1",
        }
        arguments.update(overrides)
        return ConvergenceCampaignDriver(**arguments)

    def open_campaign(self, driver: ConvergenceCampaignDriver, **overrides) -> dict:
        arguments = {
            "domain": "ui-fidelity",
            "source_path": self._target_file(),
            "target_kind": "design-doc",
            "snapshot_relative_path": "target.md",
            "base_commit": "0" * 40,
            "pre_journal_sanitizer": (
                "scripts.run_convergence_campaign:identity_pre_journal_sanitizer"
            ),
            "recall_threshold": 0.8,
            "amendment_ratio_threshold": 0.2,
        }
        arguments.update(overrides)
        return driver.open_campaign(**arguments)

    def _target_file(self) -> Path:
        target = self.root / "target.md"
        if not target.exists():
            target.write_text("Target: build feature.\n", encoding="utf-8")
        return target


# ---------------------------------------------------------------------------
# AC-CC04-1: findings-owners-paths table, unacknowledged-warning refusal,
# never writes operator-approval.json
# ---------------------------------------------------------------------------


class UnacknowledgedWarningsHelperTests(unittest.TestCase):
    def test_a_warning_absent_from_acknowledgements_is_outstanding(self) -> None:
        gate_evidence = {
            "warnings": [
                {"kind": "sibling-allowed-path-overlap", "severity": "info", "runs": ["A", "B"], "paths": ["x"]},
            ]
        }
        outstanding = unacknowledged_warnings(gate_evidence, [])
        self.assertEqual(len(outstanding), 1)

    def test_a_matching_acknowledgement_clears_it(self) -> None:
        from harness_labs.plangraph.plan_approval import warning_identity

        warning = {"kind": "sibling-allowed-path-overlap", "severity": "info", "runs": ["A", "B"], "paths": ["x"]}
        gate_evidence = {"warnings": [warning]}
        outstanding = unacknowledged_warnings(
            gate_evidence, [{"warning_sha256": warning_identity(warning), "reason": "ack"}],
        )
        self.assertEqual(outstanding, ())


class ApprovalPacketTests(_RepoFixture):
    def _decomposition(self) -> dict:
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Build feature.txt. AC-1: feature works."},
            "acceptance_criteria": {"AC-1": "feature works."},
            "runs": [
                {
                    "id": "A",
                    "objective": "Build feature.txt. AC-1: feature works.",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": ["a/x.py", "shared"],
                    "path_intents": [{"path": "a/x.py", "action": "create"}],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
                {
                    "id": "B",
                    "objective": "Build feature.txt. AC-1: feature works.",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": ["b/y.py", "shared"],
                    "path_intents": [{"path": "b/y.py", "action": "create"}],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def _commit_decomposition(self) -> Path:
        (self.repository / "docs").mkdir(exist_ok=True)
        (self.repository / "docs" / "plan.md").write_text(
            "Build feature.txt. AC-1: feature works.\n", encoding="utf-8"
        )
        decomposition = self.repository / "decomposition.json"
        decomposition.write_text(
            json.dumps(self._decomposition(), sort_keys=True) + "\n", encoding="utf-8"
        )
        self._git("add", ".")
        self._git("commit", "-m", "add plan and decomposition")
        return decomposition

    def _findings_by_run(self) -> dict:
        return {
            "A": [_finding("a/x.py", "missing-x")],
            "B": [_finding("b/y.py", "missing-y")],
        }

    def test_packet_includes_every_sibling_overlap_warning(self) -> None:
        decomposition_path = self._commit_decomposition()
        driver = self.driver()
        self.open_campaign(driver)
        output = self.root / "approval"
        try:
            driver.approve_prepare(
                repository=self.repository,
                decomposition_path=decomposition_path,
                output_directory=output,
                findings_by_run=self._findings_by_run(),
            )
            self.fail("expected UnacknowledgedWarningsError before any warning is acknowledged")
        except UnacknowledgedWarningsError:
            gate_evidence = json.loads((output / "gate-evidence.json").read_text(encoding="utf-8"))
            overlap_warnings = sibling_overlap_warnings_from_gate_evidence(gate_evidence)
            self.assertEqual(len(overlap_warnings), 1)
            self.assertEqual(set(overlap_warnings[0]["runs"]), {"A", "B"})
            self.assertIn("shared", overlap_warnings[0]["paths"])

    def test_any_severity_warning_blocks_not_only_high(self) -> None:
        """The driver's own gate is stricter than ``issue_receipt``'s: it
        refuses on any unacknowledged warning, not only high-severity ones."""

        decomposition_path = self._commit_decomposition()
        driver = self.driver()
        self.open_campaign(driver)
        output = self.root / "approval"
        prepared_gate_evidence_path = output / "gate-evidence.json"
        with self.assertRaises(UnacknowledgedWarningsError):
            driver.approve_prepare(
                repository=self.repository,
                decomposition_path=decomposition_path,
                output_directory=output,
                findings_by_run=self._findings_by_run(),
            )
        gate_evidence = json.loads(prepared_gate_evidence_path.read_text(encoding="utf-8"))
        warning = gate_evidence["warnings"][0]
        self.assertEqual(warning["severity"], "info")  # shared directory grant, not a filename collision

    def test_packet_renders_once_every_warning_is_acknowledged(self) -> None:
        decomposition_path = self._commit_decomposition()
        driver = self.driver()
        self.open_campaign(driver)
        output = self.root / "approval-attempt-1"
        with self.assertRaises(UnacknowledgedWarningsError):
            driver.approve_prepare(
                repository=self.repository,
                decomposition_path=decomposition_path,
                output_directory=output,
                findings_by_run=self._findings_by_run(),
            )
        from harness_labs.plangraph.plan_approval import warning_identity

        gate_evidence = json.loads((output / "gate-evidence.json").read_text(encoding="utf-8"))
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "intentional shared grant"}
            for warning in gate_evidence["warnings"]
        ]
        output_2 = self.root / "approval-attempt-2"
        packet = driver.approve_prepare(
            repository=self.repository,
            decomposition_path=decomposition_path,
            output_directory=output_2,
            findings_by_run=self._findings_by_run(),
            warning_acknowledgements=acknowledgements,
        )
        self.assertIsInstance(packet, ApprovalPacket)
        self.assertEqual(len(packet.findings_table), 2)
        self.assertEqual({row["run_id"] for row in packet.findings_table}, {"A", "B"})

    def test_rendered_packet_object_carries_the_sibling_overlap_warning(self) -> None:
        """``AC-CC04-1``'s rendering clause proven on the ``ApprovalPacket``
        object itself, not on the helper applied to the raw gate-evidence
        file in isolation."""

        decomposition_path = self._commit_decomposition()
        driver = self.driver()
        self.open_campaign(driver)
        output = self.root / "approval-attempt-1"
        with self.assertRaises(UnacknowledgedWarningsError):
            driver.approve_prepare(
                repository=self.repository,
                decomposition_path=decomposition_path,
                output_directory=output,
                findings_by_run=self._findings_by_run(),
            )
        from harness_labs.plangraph.plan_approval import warning_identity

        gate_evidence = json.loads((output / "gate-evidence.json").read_text(encoding="utf-8"))
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "intentional shared grant"}
            for warning in gate_evidence["warnings"]
        ]
        output_2 = self.root / "approval-attempt-2"
        packet = driver.approve_prepare(
            repository=self.repository,
            decomposition_path=decomposition_path,
            output_directory=output_2,
            findings_by_run=self._findings_by_run(),
            warning_acknowledgements=acknowledgements,
        )
        self.assertEqual(len(packet.warnings), len(gate_evidence["warnings"]))
        self.assertEqual(len(packet.sibling_overlap_warnings), 1)
        self.assertEqual(set(packet.sibling_overlap_warnings[0]["runs"]), {"A", "B"})
        self.assertIn("shared", packet.sibling_overlap_warnings[0]["paths"])

    def test_driver_never_writes_operator_approval_itself(self) -> None:
        decomposition_path = self._commit_decomposition()
        driver = self.driver()
        self.open_campaign(driver)
        probe_output = self.root / "approval-probe"
        from harness_labs.plangraph.plan_approval import warning_identity

        try:
            driver.approve_prepare(
                repository=self.repository,
                decomposition_path=decomposition_path,
                output_directory=probe_output,
                findings_by_run=self._findings_by_run(),
            )
        except UnacknowledgedWarningsError:
            pass
        gate_evidence = json.loads((probe_output / "gate-evidence.json").read_text(encoding="utf-8"))
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "ack"}
            for warning in gate_evidence["warnings"]
        ]
        output = self.root / "approval-final"
        driver.approve_prepare(
            repository=self.repository,
            decomposition_path=decomposition_path,
            output_directory=output,
            findings_by_run=self._findings_by_run(),
            warning_acknowledgements=acknowledgements,
        )
        self.assertEqual(list(output.glob("operator-approval*")), [])
        self.assertEqual(list(output.glob("operator*.json")), [])

        # And the issue step refuses outright while the file is absent.
        with self.assertRaises(ConvergenceCampaignDriverError):
            issue_approval(
                repository=self.repository,
                subject_path=output / "subject.json",
                gate_evidence_path=output / "gate-evidence.json",
                operator_approval_path=output / "operator-approval.json",
                receipt_path=output / "receipt.json",
            )
        self.assertFalse((output / "operator-approval.json").exists())

    def _high_severity_decomposition(self) -> dict:
        """Two runs claiming the *same specific file* (not merely a shared
        directory) in their own ``allowed_paths`` -- the sibling-overlap
        rule's high-signal case (``plan_approval.py``'s ``severity`` picks
        ``"high"`` only when the shared path is file-looking and directly
        held by both runs)."""

        decomposition = self._decomposition()
        decomposition["runs"][0]["allowed_paths"] = ["a/x.py", "shared/collide.py"]
        decomposition["runs"][0]["path_intents"].append({"path": "shared/collide.py", "action": "create"})
        decomposition["runs"][1]["allowed_paths"] = ["b/y.py", "shared/collide.py"]
        decomposition["runs"][1]["path_intents"].append({"path": "shared/collide.py", "action": "create"})
        return decomposition

    def test_prepare_with_acks_drives_through_issue_to_a_valid_receipt(self) -> None:
        """``AC-CC04-1`` end-to-end: acknowledging every warning (here, a
        high-severity file collision -- the case ``issue_receipt``'s own
        acknowledgment gate actually recognizes) through a real,
        human-shaped ``operator-approval.json`` must actually reach a
        receipt, with that exact file -- byte-for-byte, never a driver
        -authored substitute -- bound as the receipt's own
        ``operator_approval`` reference."""

        from harness_labs.plangraph.plan_approval import (
            OPERATOR_APPROVAL_PROTOCOL,
            PlanApprovalAdmission,
            warning_identity,
        )
        from harness_labs.plangraph.plan_graph_contract import sha256_json

        decomposition = self._high_severity_decomposition()
        (self.repository / "docs").mkdir(exist_ok=True)
        (self.repository / "docs" / "plan.md").write_text(
            "Build feature.txt. AC-1: feature works.\n", encoding="utf-8"
        )
        decomposition_path = self.repository / "decomposition.json"
        decomposition_path.write_text(json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "add plan and decomposition")

        driver = self.driver()
        self.open_campaign(driver)
        probe_output = self.root / "approval-probe-2"
        with self.assertRaises(UnacknowledgedWarningsError):
            driver.approve_prepare(
                repository=self.repository,
                decomposition_path=decomposition_path,
                output_directory=probe_output,
                findings_by_run=self._findings_by_run(),
            )
        gate_evidence = json.loads((probe_output / "gate-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(len(gate_evidence["warnings"]), 1)
        self.assertEqual(gate_evidence["warnings"][0]["severity"], "high")
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "ack"}
            for warning in gate_evidence["warnings"]
        ]

        output = self.root / "approval-issued"
        packet = driver.approve_prepare(
            repository=self.repository,
            decomposition_path=decomposition_path,
            output_directory=output,
            findings_by_run=self._findings_by_run(),
            warning_acknowledgements=acknowledgements,
        )
        self.assertIsInstance(packet, ApprovalPacket)

        subject = json.loads((output / "subject.json").read_text(encoding="utf-8"))
        operator_approval_path = output / "operator-approval.json"
        operator_approval_path.write_text(
            json.dumps(
                {
                    "protocol": OPERATOR_APPROVAL_PROTOCOL,
                    "subject_sha256": sha256_json(subject),
                    "actor": "operator",
                    "approved_at": "2026-01-01T00:00:00Z",
                    "statement": "approved",
                    "warning_acknowledgements": acknowledgements,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        original_operator_approval_bytes = operator_approval_path.read_bytes()

        receipt_path = driver.approve_issue(
            repository=self.repository,
            subject_path=output / "subject.json",
            gate_evidence_path=output / "gate-evidence.json",
            operator_approval_path=operator_approval_path,
            receipt_path=output / "receipt.json",
        )
        self.assertTrue(receipt_path.exists())

        # The human-authored file is untouched, byte-for-byte.
        self.assertEqual(operator_approval_path.read_bytes(), original_operator_approval_bytes)

        # No driver-authored substitute file was written beside the receipt.
        self.assertEqual(list(output.glob("*operator-approval-high-severity.json")), [])

        # The receipt's own operator_approval reference resolves to the
        # operator's own file itself -- not a filtered copy -- and it still
        # exists (not a deleted temp file) and re-validates.
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        referenced = (receipt_path.parent / receipt["operator_approval"]["path"]).resolve()
        self.assertEqual(referenced, operator_approval_path.resolve())
        self.assertTrue(referenced.exists())
        validated = PlanApprovalAdmission(repository=self.repository, receipt_path=receipt_path).validate()
        self.assertEqual(validated.subject_sha256, sha256_json(subject))


# ---------------------------------------------------------------------------
# AC-CC04-8: byte-identity criteria text and objective-in-plan-text
# ---------------------------------------------------------------------------


class ByteIdentityTests(unittest.TestCase):
    def test_criteria_text_mismatch_is_reported(self) -> None:
        decomposition = {"acceptance_criteria": {"F1": "the button must be blue."}}
        violations = check_criteria_byte_identity(
            decomposition, {"repair-1": [{"id": "F1", "text": "the button must be BLUE."}]}
        )
        self.assertEqual(violations, ("repair-1:F1",))

    def test_criteria_text_byte_identical_passes(self) -> None:
        decomposition = {"acceptance_criteria": {"F1": "the button must be blue."}}
        violations = check_criteria_byte_identity(
            decomposition, {"repair-1": [{"id": "F1", "text": "the button must be blue."}]}
        )
        self.assertEqual(violations, ())

    def test_unknown_criterion_id_is_a_violation(self) -> None:
        decomposition = {"acceptance_criteria": {"F1": "text"}}
        violations = check_criteria_byte_identity(
            decomposition, {"repair-1": [{"id": "F9", "text": "text"}]}
        )
        self.assertEqual(violations, ("repair-1:F9",))

    def test_extract_plan_section_stops_at_next_equal_or_shallower_heading(self) -> None:
        plan_text = (
            "# Title\n"
            "## Section A\n"
            "Body of A.\n"
            "More of A.\n"
            "## Section B\n"
            "Body of B.\n"
        )
        section = extract_plan_section(plan_text, "## Section A")
        self.assertIn("Body of A.", section)
        self.assertNotIn("Body of B.", section)

    def test_extract_plan_section_missing_heading_returns_empty(self) -> None:
        self.assertEqual(extract_plan_section("# Only heading\nbody\n", "## Absent"), "")

    def test_objective_present_in_cited_section_passes(self) -> None:
        plan_text = "## Section A\nThe button must be blue per the design system.\n"
        decomposition = {
            "plan_sections": {"a": "## Section A"},
            "runs": [
                {
                    "id": "repair-1",
                    "objective": "The button must be blue per the design system.",
                    "plan_sections": ["a"],
                }
            ],
        }
        self.assertEqual(check_objective_in_plan_text(decomposition, plan_text), ())

    def test_objective_absent_from_cited_section_is_a_violation(self) -> None:
        plan_text = "## Section A\nThe button must be blue per the design system.\n"
        decomposition = {
            "plan_sections": {"a": "## Section A"},
            "runs": [
                {
                    "id": "repair-1",
                    "objective": "Rewrite the entire navigation stack.",
                    "plan_sections": ["a"],
                }
            ],
        }
        self.assertEqual(check_objective_in_plan_text(decomposition, plan_text), ("repair-1",))

    def test_render_approval_packet_refuses_on_criteria_mismatch(self) -> None:
        with _MinimalApprovalFixture() as fixture:
            commit_count_before = fixture.commit_count()
            with self.assertRaises(ByteIdentityViolation):
                render_approval_packet(
                    repository=fixture.repository,
                    decomposition_path=fixture.decomposition_path,
                    output_directory=fixture.output,
                    findings_by_run={},
                    criteria_texts_by_run={"A": [{"id": "AC-1", "text": "wrong text entirely"}]},
                )
            # A criteria-byte-identity refusal needs neither the
            # findings-owners-paths table nor admission to have run yet, so
            # it must not leave a commit in the operator's repository.
            self.assertEqual(fixture.commit_count(), commit_count_before)

    def test_render_approval_packet_refuses_when_objective_absent_from_plan(self) -> None:
        with _MinimalApprovalFixture() as fixture:
            commit_count_before = fixture.commit_count()
            with self.assertRaises(ByteIdentityViolation):
                render_approval_packet(
                    repository=fixture.repository,
                    decomposition_path=fixture.decomposition_path,
                    output_directory=fixture.output,
                    findings_by_run={},
                    plan_text="## Section 1\nSomething unrelated entirely.\n",
                )
            self.assertEqual(fixture.commit_count(), commit_count_before)

    def test_render_approval_packet_objective_check_reads_the_working_tree_plan_when_no_plan_text_given(
        self,
    ) -> None:
        """The objective-in-plan-text check needs no ``base_commit`` from
        admission: it reads the pristine worktree's own plan file directly,
        so it can run (and refuse, committing nothing) before the
        findings-owners-paths table is ever committed."""

        with _MinimalApprovalFixture() as fixture:
            fixture.decomposition_path.write_text(
                json.dumps(
                    {**json.loads(fixture.decomposition_path.read_text()), "runs": [
                        {
                            "id": "A",
                            "objective": "This objective is not in the plan at all.",
                            "plan_sections": ["1"],
                            "criteria": ["AC-1"],
                            "depends_on": [],
                            "allowed_paths": ["feature.txt"],
                            "path_intents": [{"path": "feature.txt", "action": "create"}],
                            "verification_argv": [sys.executable, "-c", "pass"],
                            "verification_timeout_seconds": 30,
                            "verification_required_paths": [],
                        }
                    ]},
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(fixture.repository), "add", "."], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(fixture.repository), "commit", "-m", "amend objective"],
                check=True, capture_output=True,
            )
            commit_count_before = fixture.commit_count()

            with self.assertRaises(ByteIdentityViolation):
                render_approval_packet(
                    repository=fixture.repository,
                    decomposition_path=fixture.decomposition_path,
                    output_directory=fixture.output,
                    findings_by_run={},
                )
            self.assertEqual(fixture.commit_count(), commit_count_before)


class _MinimalApprovalFixture:
    """A single-run, warning-free repository for the byte-identity tests."""

    def __enter__(self) -> "_MinimalApprovalFixture":
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.repository = root / "repository"
        self.repository.mkdir()
        self.output = root / "approval"
        subprocess.run(["git", "-C", str(self.repository), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repository), "config", "user.email", "t@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repository), "config", "user.name", "T"], check=True, capture_output=True)
        (self.repository / ".harness").mkdir()
        (self.repository / ".harness" / "repository.json").write_text(
            json.dumps({"protocol": "harness-repository-identity/1", "repository_id": "byte-identity-fixture"}) + "\n",
            encoding="utf-8",
        )
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "plan.md").write_text(
            "## Section 1\nBuild feature.txt.\n", encoding="utf-8"
        )
        decomposition = {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "## Section 1"},
            "acceptance_criteria": {"AC-1": "feature works."},
            "runs": [
                {
                    "id": "A",
                    "objective": "Build feature.txt.",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": ["feature.txt"],
                    "path_intents": [{"path": "feature.txt", "action": "create"}],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                }
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }
        self.decomposition_path = self.repository / "decomposition.json"
        self.decomposition_path.write_text(json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repository), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-m", "add plan"], check=True, capture_output=True)
        return self

    def __exit__(self, *exc) -> None:
        self._temporary.cleanup()

    def commit_count(self) -> int:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "rev-list", "--count", "HEAD"],
            text=True, capture_output=True, check=True,
        )
        return int(completed.stdout.strip())


# ---------------------------------------------------------------------------
# AC-CC04-2: both-block-path harvest, base-adoption
# ---------------------------------------------------------------------------


def _review_ledger_doc(findings: dict) -> dict:
    return {
        "protocol": "review-ledger/1",
        "policy": {},
        "risk_tier": "mechanical",
        "findings": findings,
        "cycles": [],
    }


class HarvestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.attempt_dir = Path(self.temporary.name) / "attempt"
        self.attempt_dir.mkdir()

    def _write_review_ledger(self, run_dir: Path, findings: dict, *, number: int = 1) -> None:
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / f"{number:06d}-review-ledger.json").write_text(
            json.dumps(_review_ledger_doc(findings)), encoding="utf-8"
        )

    def _write_escalation_and_checkpoint(self, nodes: list, node_run_dirs: dict) -> None:
        (self.attempt_dir / "escalation.json").write_text(
            json.dumps({"protocol": "plan-graph-block-escalation/1", "nodes": nodes}), encoding="utf-8",
        )
        (self.attempt_dir / "checkpoint.json").write_text(
            json.dumps(
                {
                    "state": {
                        "nodes": {
                            node_id: {"run_dir": str(run_dir)}
                            for node_id, run_dir in node_run_dirs.items()
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_harvests_open_findings_on_the_child_blocked_path(self) -> None:
        """No retained candidate: the FeatureRun's own report blocked."""

        run_dir = Path(self.temporary.name) / "run-alpha"
        self._write_review_ledger(
            run_dir,
            {
                "k1": {
                    "key": "k1", "file": "src/a.py", "subject": "missing null check",
                    "outcome": "open", "required_paths": ["src/a.py"],
                },
                "k2": {
                    "key": "k2", "file": "src/a.py", "subject": "already fixed",
                    "outcome": "fixed", "required_paths": ["src/a.py"],
                },
            },
        )
        self._write_escalation_and_checkpoint(
            nodes=[{"node_id": "alpha", "status": "blocked", "candidate_commit": None}],
            node_run_dirs={"alpha": run_dir},
        )

        harvested = harvest_unrouted_findings(self.attempt_dir)

        self.assertEqual(len(harvested), 1)
        self.assertIsInstance(harvested[0], HarvestedFinding)
        self.assertEqual(harvested[0].block_path, "child_blocked")
        self.assertEqual(harvested[0].source_node_id, "alpha")
        self.assertEqual(harvested[0].finding["file"], "src/a.py")
        self.assertEqual(harvested[0].finding["subject"], "missing null check")
        self.assertIn("src/a.py", harvested[0].finding["required_paths"])

    def test_harvests_open_findings_on_the_transfer_conflict_path(self) -> None:
        """A retained, verified candidate: ``candidate_commit`` is present."""

        run_dir = Path(self.temporary.name) / "run-beta"
        self._write_review_ledger(
            run_dir,
            {
                "k1": {
                    "key": "k1", "file": "src/b.py", "subject": "pending_review finding",
                    "outcome": "pending_review", "required_paths": ["src/b.py"],
                },
            },
        )
        self._write_escalation_and_checkpoint(
            nodes=[{"node_id": "beta", "status": "blocked", "candidate_commit": "c" * 40}],
            node_run_dirs={"beta": run_dir},
        )

        harvested = harvest_unrouted_findings(self.attempt_dir)

        self.assertEqual(len(harvested), 1)
        self.assertEqual(harvested[0].block_path, "transfer_conflict")
        self.assertEqual(harvested[0].source_node_id, "beta")

    def test_harvests_both_paths_in_one_blocked_attempt(self) -> None:
        run_dir_a = Path(self.temporary.name) / "run-alpha"
        run_dir_b = Path(self.temporary.name) / "run-beta"
        self._write_review_ledger(
            run_dir_a, {"k1": {"file": "src/a.py", "subject": "a-issue", "outcome": "open"}}
        )
        self._write_review_ledger(
            run_dir_b, {"k1": {"file": "src/b.py", "subject": "b-issue", "outcome": "open"}}
        )
        self._write_escalation_and_checkpoint(
            nodes=[
                {"node_id": "alpha", "status": "blocked", "candidate_commit": None},
                {"node_id": "beta", "status": "blocked", "candidate_commit": "d" * 40},
                {"node_id": "gamma", "status": "succeeded", "candidate_commit": "e" * 40},
            ],
            node_run_dirs={"alpha": run_dir_a, "beta": run_dir_b, "gamma": Path(self.temporary.name) / "run-gamma"},
        )

        harvested = harvest_unrouted_findings(self.attempt_dir)

        paths = {item.block_path for item in harvested}
        self.assertEqual(paths, {"child_blocked", "transfer_conflict"})
        self.assertEqual({item.source_node_id for item in harvested}, {"alpha", "beta"})

    def test_no_escalation_yields_no_harvest(self) -> None:
        self.assertEqual(harvest_unrouted_findings(self.attempt_dir), ())

    def test_using_a_real_plan_graph_block_produces_readable_escalation(self) -> None:
        """Grounding check: a genuine ``PlanGraph`` block produces exactly the
        ``escalation.json``/``checkpoint.json`` shape the harvest function
        reads, on the ``child_blocked`` path."""

        repo_root = Path(self.temporary.name) / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "-C", str(repo_root), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "t@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "T"], check=True, capture_output=True)
        (repo_root / "plan.md").write_text("AC-1", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_root), "add", "plan.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_root), "commit", "-m", "plan"], check=True, capture_output=True)
        base_commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True, capture_output=True, check=True,
        ).stdout.strip()
        decomposition = {
            "plan": "plan.md",
            "base_commit": base_commit,
            "runs": [{"id": "alpha", "objective": "alpha", "plan_sections": ["1"], "criteria": ["AC-1"]}],
            "plan_sections": {"1": "AC-1"},
            "acceptance_criteria": {"AC-1": "AC-1"},
        }
        registration = register_plan_graph(
            repository=repo_root, logical_graph_id="logical", decomposition=decomposition,
        )
        run_root = Path(self.temporary.name) / "runs"
        graph = PlanGraph(
            repo_root, registration,
            lambda request: FeatureRunOutcome("blocked", evidence={"error": "no fix available"}),
            run_root=run_root, graph_run_id="attempt-real",
        )
        self.assertEqual(graph.run().status, "blocked")
        attempt_dir = run_root / "attempt-real"

        checkpoint = json.loads((attempt_dir / "checkpoint.json").read_text(encoding="utf-8"))
        run_dir = Path(checkpoint["state"]["nodes"]["alpha"]["run_dir"])
        self._write_review_ledger(run_dir, {"k1": {"file": "x.py", "subject": "s", "outcome": "open"}})

        harvested = harvest_unrouted_findings(attempt_dir)
        self.assertEqual(len(harvested), 1)
        self.assertEqual(harvested[0].block_path, "child_blocked")
        self.assertEqual(harvested[0].source_node_id, "alpha")


class BaseAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.attempt_dir = Path(self.temporary.name)

    def test_success_status_adopts_the_candidate(self) -> None:
        adopted, base = base_adoption_decision(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            attempt_dir=self.attempt_dir,
            join_node_id="join",
        )
        self.assertTrue(adopted)
        self.assertEqual(base, "a" * 40)

    def test_blocked_status_with_join_sealed_adopts_its_candidate(self) -> None:
        (self.attempt_dir / "checkpoint.json").write_text(
            json.dumps(
                {"state": {"nodes": {"join": {"status": "succeeded", "candidate_commit": "b" * 40}}}}
            ),
            encoding="utf-8",
        )
        adopted, base = base_adoption_decision(
            run_result={"status": "blocked"}, attempt_dir=self.attempt_dir, join_node_id="join",
        )
        self.assertTrue(adopted)
        self.assertEqual(base, "b" * 40)

    def test_blocked_status_with_join_not_sealed_keeps_current_base(self) -> None:
        (self.attempt_dir / "checkpoint.json").write_text(
            json.dumps({"state": {"nodes": {"join": {"status": "blocked"}}}}), encoding="utf-8",
        )
        adopted, base = base_adoption_decision(
            run_result={"status": "blocked"}, attempt_dir=self.attempt_dir, join_node_id="join",
        )
        self.assertFalse(adopted)
        self.assertIsNone(base)

    def test_blocked_status_with_no_join_node_keeps_current_base(self) -> None:
        adopted, base = base_adoption_decision(
            run_result={"status": "blocked"}, attempt_dir=self.attempt_dir, join_node_id=None,
        )
        self.assertFalse(adopted)
        self.assertIsNone(base)


class JoinRegressionNodeTests(unittest.TestCase):
    def test_finds_the_unique_sink_that_depends_on_everything(self) -> None:
        runs = [
            {"id": "repair-a", "depends_on": []},
            {"id": "repair-b", "depends_on": []},
            {"id": "join", "depends_on": ["repair-a", "repair-b"]},
        ]
        self.assertEqual(join_regression_node_id(runs), "join")

    def test_single_run_round_is_its_own_join_node(self) -> None:
        self.assertEqual(join_regression_node_id([{"id": "solo", "depends_on": []}]), "solo")

    def test_raises_when_no_unique_join_node_exists(self) -> None:
        runs = [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": []}]
        with self.assertRaises(ConvergenceCampaignDriverError):
            join_regression_node_id(runs)


class ValidateRoundGrantsTests(unittest.TestCase):
    def _decomposition(self, *, overlap: bool) -> dict:
        second_paths = ["repair/b.py"] if not overlap else ["repair/a.py"]
        return {
            "runs": [
                {"id": "repair-a", "depends_on": [], "allowed_paths": ["repair/a.py"]},
                {"id": "repair-b", "depends_on": [], "allowed_paths": second_paths},
                {"id": "join", "depends_on": ["repair-a", "repair-b"], "allowed_paths": ["repair/a.py", "repair/b.py"]},
            ]
        }

    def _findings_by_run(self, *, overlap: bool) -> dict:
        return {
            "repair-a": [_finding("repair/a.py", "s1")],
            "repair-b": [_finding("repair/b.py" if not overlap else "repair/a.py", "s2")],
        }

    def test_passes_when_grants_match_owned_findings_and_are_disjoint(self) -> None:
        validate_round_grants(self._decomposition(overlap=False), self._findings_by_run(overlap=False), "join")

    def test_raises_on_grant_overlap_between_unordered_repair_nodes(self) -> None:
        with self.assertRaises(ConvergenceCampaignDriverError):
            validate_round_grants(self._decomposition(overlap=True), self._findings_by_run(overlap=True), "join")

    def test_raises_when_grants_do_not_equal_owned_required_paths(self) -> None:
        decomposition = self._decomposition(overlap=False)
        findings_by_run = {"repair-a": [_finding("other/path.py", "s1")], "repair-b": []}
        with self.assertRaises(ConvergenceCampaignDriverError):
            validate_round_grants(decomposition, findings_by_run, "join")


# ---------------------------------------------------------------------------
# AC-CC04-3: repair-round bound with the audit outside it
# ---------------------------------------------------------------------------


class RepairRoundBudgetTests(unittest.TestCase):
    def test_a_fourth_plan_step_is_blocked_by_default(self) -> None:
        budget = RepairRoundBudget()
        for _ in range(3):
            budget.record_plan_step()
        self.assertFalse(budget.permits_plan_step())
        with self.assertRaises(RepairRoundBoundExceeded):
            budget.record_plan_step()

    def test_guard_before_plan_raises_bound_exceeded_with_no_stall(self) -> None:
        ledger = ConvergenceLedger(Path(tempfile.mkdtemp()) / "ledger.jsonl")
        budget = RepairRoundBudget(max_repair_rounds=1, repair_rounds_used=1)
        with self.assertRaises(RepairRoundBoundExceeded):
            guard_before_plan(ledger=ledger, budget=budget)


class DriverRoundBoundTests(_RepoFixture):
    def _decomposition(self, run_id: str = "solo") -> dict:
        return {"runs": [{"id": run_id, "depends_on": [], "allowed_paths": ["x.py"]}]}

    def test_plan_step_is_blocked_after_the_bound_but_measure_never_is(self) -> None:
        driver = self.driver(max_repair_rounds=2)
        self.open_campaign(driver)
        findings_by_run = {"solo": []}
        driver.plan(decomposition=self._decomposition(), findings_by_run=findings_by_run)
        driver.plan(decomposition=self._decomposition(), findings_by_run=findings_by_run)
        with self.assertRaises(RepairRoundBoundExceeded):
            driver.plan(decomposition=self._decomposition(), findings_by_run=findings_by_run)

        # measure is never gated by the repair-round bound.
        capture_argv, out_dir = self._capture_argv(
            {"digest": "d1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )
        result = driver.measure(capture_argv=capture_argv, out_dir=out_dir)
        self.assertEqual(result["digest"], self._sha_for(result["audit_result"]))

    @staticmethod
    def _sha_for(payload: dict) -> str:
        import hashlib

        raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def test_close_always_permits_the_next_measure(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        result = driver.close(run_result={"status": "succeeded", "candidate_commit": "a" * 40})
        self.assertTrue(result["auto_launch_measure"])


class MeasureReceiptContractTests(_RepoFixture):
    """``measure`` must resolve the audit result from the capture run's
    ``--out`` directory (``<out>/receipt.json``), matching the shipped
    ``scripts/ui_fidelity_capture.py`` contract -- which writes its receipt
    to that file and prints nothing to stdout on success -- rather than
    parsing the subprocess's stdout as the audit result."""

    _PAYLOAD = {
        "digest": "receipt-1", "findings": [], "verdicts": [],
        "confirmed_good": [], "capture_coverage": {},
    }

    def test_measure_reads_the_audit_result_from_out_dir_receipt_json(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        out_dir = self.root / "capture-out"
        argv = _fake_capture_argv(out_dir, self._PAYLOAD)

        result = driver.measure(capture_argv=argv, out_dir=out_dir)

        self.assertEqual(result["audit_result"], self._PAYLOAD)
        self.assertEqual(len(result["digest"]), 64)

    def test_measure_ignores_noisy_non_json_stdout(self) -> None:
        """A capture command that logs progress to stdout (the real CLI's
        stub-vs-real-browser resolution, for instance) must not make
        ``measure`` crash trying to parse that text as JSON -- it never
        looks at stdout for the audit result at all."""

        driver = self.driver()
        self.open_campaign(driver)
        out_dir = self.root / "capture-out-noisy"
        argv = _fake_capture_argv(out_dir, self._PAYLOAD, noisy_stdout=True)

        result = driver.measure(capture_argv=argv, out_dir=out_dir)

        self.assertEqual(result["audit_result"], self._PAYLOAD)

    def test_measure_raises_a_clear_error_when_no_receipt_was_written(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        out_dir = self.root / "capture-out-missing"
        argv = _fake_capture_argv(out_dir, self._PAYLOAD, skip_receipt=True)

        with self.assertRaises(ConvergenceCampaignDriverError):
            driver.measure(capture_argv=argv, out_dir=out_dir)

    def test_measure_still_seals_evidence_sources_reading_from_the_receipt(self) -> None:
        """The ``evidence_sources`` sealing side effect (unchanged semantics)
        still fires when the audit result is sourced from ``receipt.json``
        rather than stdout."""

        import hashlib

        driver = self.driver()
        self.open_campaign(driver)
        evidence_file = self.root / "screenshot.png"
        evidence_file.write_bytes(b"fake-image-bytes")
        out_dir = self.root / "capture-out-evidence"
        payload = {
            "digest": "receipt-evidence",
            "findings": [_finding("a.py", "s1", evidence_refs=["ev-1"])],
            "verdicts": [], "confirmed_good": [], "capture_coverage": {},
        }
        argv = _fake_capture_argv(out_dir, payload)

        result = driver.measure(
            capture_argv=argv, out_dir=out_dir,
            evidence_sources={"ev-1": evidence_file},
        )

        self.assertEqual(result["audit_result"], payload)
        expected_digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        self.assertTrue(driver.artifacts.contains(expected_digest))

    def test_measure_sanitizer_receives_raw_receipt_text_before_journaling(self) -> None:
        """The sanitizer hook runs on the *file's* raw text, not a
        stdout-derived string, and the sanitized text is what gets parsed
        as the audit result."""

        module_reference = "scripts.run_convergence_campaign:identity_pre_journal_sanitizer"
        driver = self.driver()
        self.open_campaign(driver, pre_journal_sanitizer=module_reference)
        out_dir = self.root / "capture-out-sanitized"
        argv = _fake_capture_argv(out_dir, self._PAYLOAD)

        result = driver.measure(capture_argv=argv, out_dir=out_dir)

        # identity_pre_journal_sanitizer is a pass-through, so the receipt's
        # own bytes round-trip unchanged into the sealed audit result.
        self.assertEqual(result["audit_result"], self._PAYLOAD)
        sealed = json.loads(driver.artifacts.open_bytes(result["digest"]))
        self.assertEqual(sealed, self._PAYLOAD)


class SanitizerMediaTypePolicyDriverTests(_RepoFixture):
    """AC-SN-4: ``sanitize_before_journaling`` resolves the ``text`` hook out
    of the mapping config form exactly as the legacy string form applies it,
    and raises :class:`SanitizerFailure` -- never ``AttributeError`` -- on a
    mapping with no ``text`` entry."""

    _HOOK_REFERENCE = "scripts.run_convergence_campaign:identity_pre_journal_sanitizer"
    _TRANSFORM_HOOK_REFERENCE = (
        "tests.test_convergence_campaign_driver:_uppercasing_pre_journal_sanitizer"
    )

    def test_mapping_form_resolves_text_hook_exactly_like_the_legacy_string(self) -> None:
        """Uses a transforming hook (not the identity hook) so the assertion
        actually exercises hook application: an implementation that resolved
        the mapping form's ``text`` entry but never invoked it would leave
        ``text`` unchanged and fail the ``assertNotEqual`` below."""

        legacy_config = {"pre_journal_sanitizer": self._TRANSFORM_HOOK_REFERENCE}
        mapping_config = {
            "pre_journal_sanitizer": {"text": self._TRANSFORM_HOOK_REFERENCE, "binary": {}},
        }
        text = "some journaled text"
        mapping_result = sanitize_before_journaling(mapping_config, text)
        legacy_result = sanitize_before_journaling(legacy_config, text)
        self.assertEqual(mapping_result, legacy_result)
        self.assertNotEqual(mapping_result, text)

    def test_mapping_form_without_text_entry_raises_sanitizer_failure_not_attribute_error(
        self,
    ) -> None:
        config = {"pre_journal_sanitizer": {"binary": {"screenshot": "reject"}}}
        with self.assertRaises(SanitizerFailure):
            sanitize_before_journaling(config, "text")

    def test_mapping_form_with_non_string_text_entry_raises_sanitizer_failure(self) -> None:
        config = {"pre_journal_sanitizer": {"text": 7, "binary": {}}}
        with self.assertRaises(SanitizerFailure):
            sanitize_before_journaling(config, "text")

    def test_a_bare_non_string_non_mapping_sanitizer_value_raises_sanitizer_failure(self) -> None:
        config = {"pre_journal_sanitizer": 7}
        with self.assertRaises(SanitizerFailure):
            sanitize_before_journaling(config, "text")

    def test_driver_measure_threads_the_mapping_form_through_open_campaign(self) -> None:
        """The mapping form survives the full ``open_campaign`` ->
        ``campaign_config`` -> ``measure`` path, not just direct calls to
        ``sanitize_before_journaling``. Uses the transforming hook so the
        assertion proves the hook actually ran on the threaded receipt text
        rather than merely being resolved and skipped: an implementation
        that dropped the hook along this path would return ``payload``
        unchanged and fail the comparison below."""

        driver = self.driver()
        self.open_campaign(
            driver,
            pre_journal_sanitizer={"text": self._TRANSFORM_HOOK_REFERENCE, "binary": {}},
        )
        payload = {
            "digest": "d-mapping", "findings": [], "verdicts": [],
            "confirmed_good": [], "capture_coverage": {},
        }
        out_dir = self.root / "capture-out-mapping"
        argv = _fake_capture_argv(out_dir, payload)

        result = driver.measure(capture_argv=argv, out_dir=out_dir)

        expected = json.loads(json.dumps(payload).upper())
        self.assertEqual(result["audit_result"], expected)
        self.assertNotEqual(result["audit_result"], payload)

    def test_config_reads_a_textless_mapping_and_raises_sanitizer_failure(self) -> None:
        """``build_campaign_config``/``pin_target`` already refuse a textless
        mapping at config-build time (AC-SN-1's config-surface validation);
        this exercises the config-read path (``campaign_config``) directly
        against a raw ``ConvergenceLedger.open_campaign`` record -- the
        "checkpoint state built outside build_campaign_config" case
        ``sanitize_before_journaling``'s own docstring names -- proving the
        read path itself, not only the write-time gate, fails closed with
        ``SanitizerFailure`` rather than an ``AttributeError``.
        """

        from scripts.run_convergence_campaign import campaign_config

        driver = self.driver()
        driver.ledger.open_campaign(
            domain="ui-fidelity",
            target={"kind": "design-doc", "digest": "e" * 64, "snapshot_path": "target.md"},
            base_commit="0" * 40,
            config={"pre_journal_sanitizer": {"binary": {"screenshot": "reject"}}},
        )

        with self.assertRaises(SanitizerFailure):
            sanitize_before_journaling(campaign_config(driver.ledger), "some text")


class DriverRunStepPreconditionsTests(_RepoFixture):
    """``driver-steps`` step 6: ``run`` refuses to dispatch a round with no
    ``--on-block-argv`` block hook, and refuses a named ``--registration``
    with no automatic-recovery authority baked in -- rather than silently
    permitting either."""

    def _run_result_completed(self) -> "_FakeCompleted":
        return _FakeCompleted({"status": "succeeded", "status_flags": {"success": True}})

    def test_missing_on_block_argv_refuses_before_dispatch(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        calls: list = []

        def runner(*args, **kwargs):
            calls.append(args)
            return self._run_result_completed()

        with self.assertRaises(ConvergenceCampaignDriverError):
            driver.run_graph(argv=["run_plan_graph.py", "run"], runner=runner)
        self.assertEqual(calls, [])

    def test_registration_missing_automatic_recovery_refuses_before_dispatch(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        registration_path = self.root / "registration.json"
        registration_path.write_text(
            json.dumps({"automatic_recovery": None}), encoding="utf-8",
        )
        calls: list = []

        def runner(*args, **kwargs):
            calls.append(args)
            return self._run_result_completed()

        with self.assertRaises(ConvergenceCampaignDriverError):
            driver.run_graph(
                argv=[
                    "run_plan_graph.py", "run", "--registration", str(registration_path),
                    "--on-block-argv", "[]",
                ],
                runner=runner,
            )
        self.assertEqual(calls, [])

    def test_on_block_argv_and_bound_recovery_authority_dispatches(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        registration_path = self.root / "registration.json"
        registration_path.write_text(
            json.dumps({"automatic_recovery": {"authority": "operator"}}), encoding="utf-8",
        )

        result = driver.run_graph(
            argv=[
                "run_plan_graph.py", "run", "--registration", str(registration_path),
                "--on-block-argv", "[]",
            ],
            runner=lambda *a, **k: self._run_result_completed(),
        )
        self.assertEqual(result["status"], "succeeded")

    def test_on_block_argv_present_with_no_registration_flag_dispatches(self) -> None:
        """The recovery-authority check only applies when ``--registration``
        is present; an ``--approval-receipt`` argv is not re-derived here."""

        driver = self.driver()
        self.open_campaign(driver)

        result = driver.run_graph(
            argv=["run_plan_graph.py", "run", "--approval-receipt", "receipt.json", "--on-block-argv", "[]"],
            runner=lambda *a, **k: self._run_result_completed(),
        )
        self.assertEqual(result["status"], "succeeded")


class DriverCloseOnBlockedPathTests(_RepoFixture):
    """``driver.close`` on the blocked path: ``auto_launch_measure`` tracks
    whether the join+regression node actually sealed (not a constant),
    ``measure`` is actually launched rather than only flagged, and repeated
    harvests of the same still-open finding never fold a synthetic audit or
    fabricate a stall (``AC-CC04-2``, ``AC-CC04-3``)."""

    def _plan(self, driver: ConvergenceCampaignDriver) -> None:
        driver.plan(
            decomposition={
                "runs": [
                    {"id": "alpha", "depends_on": [], "allowed_paths": ["src/a.py"]},
                    {"id": "join", "depends_on": ["alpha"], "allowed_paths": []},
                ]
            },
            findings_by_run={
                "alpha": [_finding("src/a.py", "missing null check")],
                "join": [],
            },
        )

    def _write_blocked_attempt(self, *, join_status: str, join_candidate: str | None) -> Path:
        attempt_dir = self.root / "attempt"
        run_dir = self.root / "run-alpha"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "000001-review-ledger.json").write_text(
            json.dumps(
                _review_ledger_doc(
                    {
                        "k1": {
                            "file": "src/a.py", "subject": "missing null check",
                            "outcome": "open", "required_paths": ["src/a.py"],
                        },
                    }
                )
            ),
            encoding="utf-8",
        )
        attempt_dir.mkdir(exist_ok=True)
        (attempt_dir / "escalation.json").write_text(
            json.dumps(
                {
                    "protocol": "plan-graph-block-escalation/1",
                    "nodes": [{"node_id": "alpha", "status": "blocked", "candidate_commit": None}],
                }
            ),
            encoding="utf-8",
        )
        join_node_state: dict = {"run_dir": str(self.root / "run-join"), "status": join_status}
        if join_candidate is not None:
            join_node_state["candidate_commit"] = join_candidate
        (attempt_dir / "checkpoint.json").write_text(
            json.dumps(
                {"state": {"nodes": {"alpha": {"run_dir": str(run_dir)}, "join": join_node_state}}}
            ),
            encoding="utf-8",
        )
        return attempt_dir

    def test_join_sealed_harvests_and_permits_measure_without_fabricating_an_audit(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        self._plan(driver)
        attempt_dir = self._write_blocked_attempt(join_status="succeeded", join_candidate="b" * 40)

        records_before = driver.ledger.records()
        closed = driver.close(run_result={"status": "blocked"}, attempt_dir=attempt_dir)

        self.assertEqual(len(closed["harvested_findings"]), 1)
        self.assertEqual(closed["harvested_findings"][0]["subject"], "missing null check")
        self.assertTrue(closed["join_sealed"])
        self.assertTrue(closed["auto_launch_measure"])
        self.assertTrue(closed["base_adopted"])
        self.assertEqual(closed["new_base_commit"], "b" * 40)

        # No synthetic audit was folded through the ledger: a harvest is not
        # a verdict sweep, and re-harvesting the same open finding on a
        # second blocked round must not read as two failed repair claims.
        self.assertEqual(driver.ledger.records(), records_before)
        self.assertEqual(driver.ledger.stalled_keys(), frozenset())

        closed_again = driver.close(run_result={"status": "blocked"}, attempt_dir=attempt_dir)
        self.assertEqual(len(closed_again["harvested_findings"]), 1)
        self.assertEqual(driver.ledger.records(), records_before)
        self.assertEqual(driver.ledger.stalled_keys(), frozenset())

    def test_harvested_findings_are_folded_into_the_next_rounds_ingest(self) -> None:
        """Harvested findings are carried in checkpoint state, not dropped:
        the next round's real ``ingest`` actually folds them in alongside
        its own genuine verdicts, and consumes them exactly once."""

        driver = self.driver()
        self.open_campaign(driver)
        self._plan(driver)
        attempt_dir = self._write_blocked_attempt(join_status="succeeded", join_candidate="b" * 40)

        closed = driver.close(run_result={"status": "blocked"}, attempt_dir=attempt_dir)
        self.assertEqual(len(closed["harvested_findings"]), 1)
        self.assertEqual(driver.state()["harvested_findings"], closed["harvested_findings"])

        harvested_key = ("src/a.py", "missing null check")
        result = driver.ingest(audit_result=_audit("d-next"))

        self.assertIn(harvested_key, {tuple(key) for key in result["summary"]["opened"]})
        self.assertEqual(driver.ledger.key_status(harvested_key), "open")
        self.assertEqual(driver.state()["harvested_findings"], [])

        # Consumed exactly once: a later ingest with nothing newly harvested
        # does not re-fold the same finding as freshly opened.
        second = driver.ingest(audit_result=_audit("d-next-2"))
        self.assertNotIn(harvested_key, {tuple(key) for key in second["summary"]["opened"]})

    def test_join_not_sealed_neither_adopts_nor_permits_measure(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        self._plan(driver)
        attempt_dir = self._write_blocked_attempt(join_status="blocked", join_candidate=None)

        closed = driver.close(run_result={"status": "blocked"}, attempt_dir=attempt_dir)

        self.assertFalse(closed["join_sealed"])
        self.assertFalse(closed["auto_launch_measure"])
        self.assertFalse(closed["base_adopted"])
        self.assertIsNone(closed["new_base_commit"])

    def test_join_sealed_with_no_usable_candidate_still_permits_measure(self) -> None:
        """``join_sealed`` is not an alias for ``base_adopted``: a join node
        that itself sealed (``status == "succeeded"``) but carries no
        ``candidate_commit`` must still permit the automatic post-repair
        measure, even though there is nothing to adopt as the next base."""

        driver = self.driver()
        self.open_campaign(driver)
        self._plan(driver)
        attempt_dir = self._write_blocked_attempt(join_status="succeeded", join_candidate=None)

        closed = driver.close(run_result={"status": "blocked"}, attempt_dir=attempt_dir)

        self.assertTrue(closed["join_sealed"])
        self.assertTrue(closed["auto_launch_measure"])
        self.assertFalse(closed["base_adopted"])
        self.assertIsNone(closed["new_base_commit"])

    def test_join_sealed_with_capture_argv_actually_launches_measure(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        self._plan(driver)
        attempt_dir = self._write_blocked_attempt(join_status="succeeded", join_candidate="c" * 40)
        capture_argv, out_dir = self._capture_argv(
            {"digest": "d9", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )

        closed = driver.close(
            run_result={"status": "blocked"}, attempt_dir=attempt_dir,
            capture_argv=capture_argv, out_dir=out_dir,
        )

        self.assertIsNotNone(closed["measure_result"])
        self.assertEqual(closed["measure_result"]["audit_result"]["digest"], "d9")
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "measured",
        )

    def test_join_not_sealed_with_capture_argv_does_not_launch_measure(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        self._plan(driver)
        attempt_dir = self._write_blocked_attempt(join_status="blocked", join_candidate=None)

        closed = driver.close(
            run_result={"status": "blocked"}, attempt_dir=attempt_dir,
            capture_argv=[sys.executable, "-c", "import json; print(json.dumps({}))"],
        )

        self.assertIsNone(closed["measure_result"])
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "closed",
        )


class DriverCloseResumeDirectiveWiringTests(_RepoFixture):
    """``driver-steps`` step 7: on the blocked-without-adoption path,
    ``close`` actually wires ``resume_directive`` into the round loop --
    carrying the round's sealed node candidates forward via the existing
    reuse path -- rather than leaving it a standalone CLI subcommand nobody
    in the loop calls."""

    def test_close_on_blocked_without_adoption_surfaces_a_resume_directive(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.plan(
            decomposition={"runs": [{"id": "alpha", "depends_on": [], "allowed_paths": ["x.py"]}]},
            findings_by_run={"alpha": []},
        )

        (self.repository / "plan.md").write_text("AC-1", encoding="utf-8")
        self._git("add", "plan.md")
        self._git("commit", "-m", "plan doc")
        base_commit = self._git("rev-parse", "HEAD")
        decomposition = {
            "plan": "plan.md", "base_commit": base_commit,
            "runs": [{"id": "alpha", "objective": "alpha", "plan_sections": ["1"], "criteria": ["AC-1"]}],
            "plan_sections": {"1": "AC-1"}, "acceptance_criteria": {"AC-1": "AC-1"},
        }
        registration = register_plan_graph(
            repository=self.repository, logical_graph_id="logical", decomposition=decomposition,
        )
        run_root = self.root / "runs"
        graph = PlanGraph(
            self.repository, registration,
            lambda request: FeatureRunOutcome("blocked", evidence={"error": "no fix"}),
            run_root=run_root, graph_run_id="attempt-1",
        )
        self.assertEqual(graph.run().status, "blocked")
        attempt_dir = run_root / "attempt-1"

        closed = driver.close(
            run_result={"status": "blocked"}, attempt_dir=attempt_dir, resume_kwargs={},
        )

        self.assertFalse(closed["base_adopted"])
        self.assertIsNotNone(closed["resume_directive"])
        self.assertIn("--resume", closed["resume_directive"]["argv"])
        self.assertEqual(
            driver.state()["next_round_resume_argv"], closed["resume_directive"]["argv"],
        )

    def test_close_without_resume_kwargs_leaves_close_unchanged(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.plan(
            decomposition={"runs": [{"id": "alpha", "depends_on": [], "allowed_paths": ["x.py"]}]},
            findings_by_run={"alpha": []},
        )
        attempt_dir = self.root / "attempt"
        attempt_dir.mkdir()
        (attempt_dir / "escalation.json").write_text(
            json.dumps(
                {
                    "protocol": "plan-graph-block-escalation/1",
                    "nodes": [{"node_id": "alpha", "status": "blocked", "candidate_commit": None}],
                }
            ),
            encoding="utf-8",
        )
        (attempt_dir / "checkpoint.json").write_text(
            json.dumps({"state": {"nodes": {"alpha": {"status": "blocked"}}}}), encoding="utf-8",
        )

        closed = driver.close(run_result={"status": "blocked"}, attempt_dir=attempt_dir)

        self.assertIsNone(closed["resume_directive"])
        self.assertNotIn("next_round_resume_argv", driver.state())


# ---------------------------------------------------------------------------
# AC-CC04-4: refuse campaign_opened on a resumable predecessor; resume from
# escalation.json
# ---------------------------------------------------------------------------


class PredecessorResumableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_root = Path(self.temporary.name)

    def test_a_blocked_manifest_is_resumable(self) -> None:
        attempt = self.run_root / "attempt-1"
        attempt.mkdir()
        (attempt / "manifest.json").write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
        self.assertTrue(predecessor_is_resumable(self.run_root, "attempt-1"))

    def test_a_failed_manifest_is_resumable(self) -> None:
        """The resume machinery's own resumable-status set is {"failed",
        "blocked"} (``scripts.plan_graph_autoresume._RESUMABLE_ATTEMPT_STATUSES``),
        not ``PlanGraph._status_flags``'s different {"blocked",
        "externally_blocked"} vocabulary."""

        attempt = self.run_root / "attempt-1"
        attempt.mkdir()
        (attempt / "manifest.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
        self.assertTrue(predecessor_is_resumable(self.run_root, "attempt-1"))

    def test_a_succeeded_manifest_is_not_resumable(self) -> None:
        attempt = self.run_root / "attempt-1"
        attempt.mkdir()
        (attempt / "manifest.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        self.assertFalse(predecessor_is_resumable(self.run_root, "attempt-1"))

    def test_a_missing_manifest_is_not_resumable(self) -> None:
        self.assertFalse(predecessor_is_resumable(self.run_root, "no-such-attempt"))


class CampaignOpenRefusalTests(_RepoFixture):
    def test_open_campaign_refuses_while_predecessor_is_resumable(self) -> None:
        predecessor_run_root = self.root / "predecessor-runs"
        attempt = predecessor_run_root / "attempt-1"
        attempt.mkdir(parents=True)
        (attempt / "manifest.json").write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
        driver = self.driver()
        with self.assertRaises(PredecessorResumableError):
            self.open_campaign(
                driver,
                predecessor_run_root=predecessor_run_root,
                predecessor_graph_id="attempt-1",
            )

    def test_open_campaign_succeeds_when_predecessor_is_not_resumable(self) -> None:
        predecessor_run_root = self.root / "predecessor-runs"
        attempt = predecessor_run_root / "attempt-1"
        attempt.mkdir(parents=True)
        (attempt / "manifest.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        driver = self.driver()
        record = self.open_campaign(
            driver, predecessor_run_root=predecessor_run_root, predecessor_graph_id="attempt-1",
        )
        self.assertEqual(record["type"], "campaign_opened")

    def test_open_campaign_succeeds_with_no_predecessor(self) -> None:
        driver = self.driver()
        record = self.open_campaign(driver)
        self.assertEqual(record["type"], "campaign_opened")


class ResumeDirectiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _git(self, repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments], text=True, capture_output=True, check=True,
        )
        return completed.stdout.strip()

    def test_resume_directive_reconstructs_every_argument_from_escalation(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        self._git(repository, "init", "-b", "main")
        self._git(repository, "config", "user.email", "t@example.com")
        self._git(repository, "config", "user.name", "T")
        (repository / "plan.md").write_text("AC-1", encoding="utf-8")
        self._git(repository, "add", "plan.md")
        self._git(repository, "commit", "-m", "plan")
        base_commit = self._git(repository, "rev-parse", "HEAD")
        decomposition = {
            "plan": "plan.md",
            "base_commit": base_commit,
            "runs": [
                {"id": "alpha", "objective": "alpha", "plan_sections": ["1"], "criteria": ["AC-1"]},
                {"id": "beta", "objective": "beta", "plan_sections": ["1"], "criteria": ["AC-1"]},
            ],
            "plan_sections": {"1": "AC-1"},
            "acceptance_criteria": {"AC-1": "AC-1"},
        }
        registration = register_plan_graph(
            repository=repository, logical_graph_id="logical", decomposition=decomposition,
        )
        run_root = self.root / "runs"
        graph = PlanGraph(
            repository, registration,
            lambda request: FeatureRunOutcome("blocked", evidence={"error": "assertion failed"}),
            run_root=run_root, graph_run_id="attempt-1", max_parallelism=2,
        )
        self.assertEqual(graph.run().status, "blocked")
        escalation = json.loads((run_root / "attempt-1" / "escalation.json").read_text(encoding="utf-8"))

        directive = resume_directive_from_escalation(run_root, "attempt-1")

        self.assertIsInstance(directive, ResumeDirective)
        self.assertEqual(directive.predecessor_attempt_id, "attempt-1")
        self.assertEqual(
            directive.logical_graph_id,
            escalation["resume_directive_template"]["logical_graph_id"],
        )
        self.assertEqual(set(directive.retry_frontier), {"alpha", "beta"})
        self.assertTrue(directive.blocker_evidence_ref.startswith("artifact:sha256:"))
        argv = directive.as_argv()
        self.assertIn("--resume", argv)
        self.assertIn("--logical-graph-id", argv)
        self.assertIn(directive.logical_graph_id, argv)
        self.assertEqual(argv.count("--retry-frontier"), 2)


# ---------------------------------------------------------------------------
# AC-CC04-5: success termination predicate
# ---------------------------------------------------------------------------


class TerminationPredicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = ConvergenceLedger(Path(self.temporary.name) / "ledger.jsonl")
        self.ledger.open_campaign(
            domain="ui-fidelity", target={"kind": "d", "digest": "e" * 64, "snapshot_path": "t.md"},
            base_commit="0" * 40,
        )

    def _evaluate(self, **overrides) -> tuple:
        captured: list[str] = []
        arguments = dict(
            ledger=self.ledger, required_cells=(), new_required_findings=0,
            inspector_recall=1.0, recall_threshold=0.8,
            amendment_ratio_threshold=0.2, amendment_ratio_acknowledged=False,
            emit=captured.append,
        )
        arguments.update(overrides)
        report = evaluate_success_termination(**arguments)
        return report, captured

    def test_all_conditions_met_succeeds(self) -> None:
        self.ledger.ingest_audit(_audit("d1", capture_coverage={"cell-1": "ok"}))
        report, printed = self._evaluate(required_cells=("cell-1",))
        self.assertTrue(report.success)
        self.assertEqual(len(printed), 1)
        self.assertIn("amendment_ratio", json.loads(printed[0]))

    def test_new_required_findings_blocks_success(self) -> None:
        report, _ = self._evaluate(new_required_findings=1)
        self.assertFalse(report.success)
        self.assertFalse(report.zero_new_required_findings)

    def test_unobserved_key_blocks_success(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding("a.py", "s1")]))
        # d2 mentions nothing, so the prior open key becomes unobserved.
        self.ledger.ingest_audit(_audit("d2"))
        report, _ = self._evaluate()
        self.assertFalse(report.success)
        self.assertFalse(report.no_unobserved)

    def test_a_freshly_opened_key_blocks_success_even_though_its_own_audit_left_nothing_unobserved(self) -> None:
        # The audit that opens a key can't also have failed to mention it,
        # so ledger.success() alone is True the moment it's ingested -- the
        # key is still merely "open", never observed_fixed or ruled, and
        # must keep blocking success via open_set().
        self.ledger.ingest_audit(_audit("d1", findings=[_finding("a.py", "s1")]))
        self.assertTrue(self.ledger.success())
        self.assertIn(("a.py", "s1"), self.ledger.open_set())
        report, _ = self._evaluate()
        self.assertFalse(report.success)
        self.assertFalse(report.no_unobserved)

    def test_missing_required_coverage_cell_blocks_success(self) -> None:
        self.ledger.ingest_audit(_audit("d1", capture_coverage={"cell-1": "unreachable"}))
        report, _ = self._evaluate(required_cells=("cell-1", "cell-2"))
        self.assertFalse(report.success)
        self.assertFalse(report.full_coverage)
        self.assertEqual(set(report.missing_coverage_cells), {"cell-1", "cell-2"})

    def test_recall_below_threshold_blocks_success(self) -> None:
        report, _ = self._evaluate(inspector_recall=0.5, recall_threshold=0.8)
        self.assertFalse(report.success)
        self.assertFalse(report.recall_ok)

    def test_amendment_ratio_above_threshold_requires_acknowledgment(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding("a.py", "amend-me")]))
        self.ledger.record_ruling(
            ("a.py", "amend-me"), disposition="amend_criterion", statement="criterion revised",
        )
        report_unacked, _ = self._evaluate(amendment_ratio_threshold=0.0, amendment_ratio_acknowledged=False)
        self.assertGreater(report_unacked.amendment_ratio, 0.0)
        self.assertFalse(report_unacked.amendment_ok)
        self.assertFalse(report_unacked.success)

        report_acked, _ = self._evaluate(amendment_ratio_threshold=0.0, amendment_ratio_acknowledged=True)
        self.assertTrue(report_acked.amendment_ok)

    def test_amendment_ratio_is_printed_at_every_termination_success_or_not(self) -> None:
        for new_findings in (0, 1):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                evaluate_success_termination(
                    ledger=self.ledger, required_cells=(), new_required_findings=new_findings,
                    inspector_recall=1.0, recall_threshold=0.8,
                    amendment_ratio_threshold=0.2, amendment_ratio_acknowledged=False,
                )
            printed = json.loads(buffer.getvalue().strip())
            self.assertIn("amendment_ratio", printed)


class DriverCloseTerminationWiringTests(_RepoFixture):
    """``AC-CC04-5``: ``evaluate_success_termination`` is actually launched
    from the step machine -- ``close``'s own ``termination_kwargs`` -- and
    from the ``close`` CLI subcommand's ``--termination-file``, not only
    callable directly by a test."""

    def test_close_evaluates_and_checkpoints_success_when_termination_kwargs_given(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)  # recall_threshold=0.8, amendment_ratio_threshold=0.2
        driver.ingest(audit_result=_audit("d1"))  # nothing open, nothing unobserved

        closed = driver.close(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            termination_kwargs={"inspector_recall": 1.0},
        )

        self.assertIsNotNone(closed["termination"])
        self.assertTrue(closed["termination"]["success"])
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "succeeded",
        )

    def test_chained_measure_then_termination_does_not_lose_the_new_pending_digest(self) -> None:
        """When ``capture_argv`` and ``termination_kwargs`` are both
        supplied, ``close`` chains ``measure`` and then
        ``evaluate_termination`` in the same call. The success-path
        checkpoint the latter saves must not clobber the
        ``pending_audit_digest`` the former just recorded with a stale,
        pre-measure snapshot of state."""

        driver = self.driver()
        self.open_campaign(driver)
        driver.ingest(audit_result=_audit("d1"))

        capture_argv, out_dir = self._capture_argv(
            {"digest": "d2", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )

        closed = driver.close(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            capture_argv=capture_argv, out_dir=out_dir,
            termination_kwargs={"inspector_recall": 1.0},
        )

        self.assertIsNotNone(closed["measure_result"])
        self.assertIsNotNone(closed["termination"])
        self.assertTrue(closed["termination"]["success"])
        checkpoint = json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["lifecycle"], "succeeded")
        self.assertEqual(checkpoint["state"]["pending_audit_digest"], closed["measure_result"]["digest"])

    def test_close_without_termination_kwargs_leaves_close_unchanged(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        closed = driver.close(run_result={"status": "succeeded", "candidate_commit": "a" * 40})
        self.assertIsNone(closed["termination"])
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "closed",
        )

    def test_close_termination_reads_recall_threshold_from_config_not_the_hardcoded_fallback(self) -> None:
        driver = self.driver()
        # Below evaluate_termination's own hardcoded fallback of 1.0: an
        # inspector_recall of 0.6 only succeeds if the *configured*
        # threshold (0.5) is actually read, not the fallback.
        self.open_campaign(driver, recall_threshold=0.5)
        driver.ingest(audit_result=_audit("d1"))

        closed = driver.close(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            termination_kwargs={"inspector_recall": 0.6},
        )
        self.assertTrue(closed["termination"]["success"])
        self.assertTrue(closed["termination"]["recall_ok"])

    def test_close_termination_reads_amendment_ratio_threshold_from_config_not_the_hardcoded_fallback(self) -> None:
        driver = self.driver()
        # Above evaluate_termination's own hardcoded fallback of 0.0: an
        # unacknowledged amendment ratio of 1.0 only succeeds if the
        # *configured* threshold (1.0) is actually read, not the fallback.
        self.open_campaign(driver, recall_threshold=0.0, amendment_ratio_threshold=1.0)
        driver.ingest(audit_result=_audit("d1", findings=[_finding("a.py", "amend-me")]))
        driver.ledger.record_ruling(
            ("a.py", "amend-me"), disposition="amend_criterion", statement="criterion revised",
        )

        closed = driver.close(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            termination_kwargs={"inspector_recall": 1.0},
        )
        self.assertEqual(closed["termination"]["amendment_ratio"], 1.0)
        self.assertTrue(closed["termination"]["amendment_ok"])
        self.assertTrue(closed["termination"]["success"])

    def test_close_termination_defaults_new_required_findings_from_the_last_ingest(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.ingest(
            audit_result=_audit(
                "d1", findings=[_finding("a.py", "s1", requires_disposition=True)],
            )
        )
        driver.rule(
            dispositions=[
                {"key": ["a.py", "s1"], "disposition": "waive", "statement": "not applicable"},
            ]
        )

        closed = driver.close(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            termination_kwargs={"inspector_recall": 1.0},
        )
        self.assertFalse(closed["termination"]["zero_new_required_findings"])
        self.assertFalse(closed["termination"]["success"])

        # An explicit override still takes precedence over the derived default.
        closed_override = driver.close(
            run_result={"status": "succeeded", "candidate_commit": "a" * 40},
            termination_kwargs={"inspector_recall": 1.0, "new_required_findings": 0},
        )
        self.assertTrue(closed_override["termination"]["zero_new_required_findings"])


class DriverBlockedTerminationAmendmentRatioTests(_RepoFixture):
    """``AC-CC04-5``: the amendment ratio is printed at every termination,
    including the blocked end states ``bounds-termination`` names -- not
    only the success path ``evaluate_termination`` reaches from ``close``."""

    def test_rule_prints_amendment_ratio_before_blocking_on_an_unanswered_ruling(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.plan(
            decomposition={
                "runs": [
                    {"id": "repair-a", "depends_on": [], "allowed_paths": ["src/a.py"]},
                    {"id": "join", "depends_on": ["repair-a"], "allowed_paths": ["src/a.py"]},
                ]
            },
            findings_by_run={"repair-a": [_finding("src/a.py", "first")]},
        )
        driver.ingest(audit_result=_audit("d1", findings=[_finding("src/a.py", "regression-candidate")]))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(ConvergenceCampaignDriverError):
                driver.rule(dispositions=[])
        printed = json.loads(buffer.getvalue().strip())
        self.assertIn("amendment_ratio", printed)

    def test_plan_prints_amendment_ratio_before_blocking_on_a_stall(self) -> None:
        driver = self.driver(max_repair_rounds=3)
        self.open_campaign(driver)
        driver.ledger.ingest_audit(_audit("d1", findings=[_finding("a.py", "s1")]))
        driver.ledger.record_fix_claimed(("a.py", "s1"))
        driver.ledger.ingest_audit(_audit("d2", verdicts=[{"key": ["a.py", "s1"], "verdict": "reopened"}]))
        driver.ledger.record_fix_claimed(("a.py", "s1"))
        driver.ledger.ingest_audit(_audit("d3", verdicts=[{"key": ["a.py", "s1"], "verdict": "reopened"}]))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(StallEscalation):
                driver.plan(
                    decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["x.py"]}]},
                    findings_by_run={"solo": []},
                )
        printed = json.loads(buffer.getvalue().strip())
        self.assertIn("amendment_ratio", printed)

    def test_measure_prints_amendment_ratio_before_a_sanitizer_failure(self) -> None:
        driver = self.driver()
        self.open_campaign(
            driver, pre_journal_sanitizer="scripts.run_convergence_campaign:no_such_hook",
        )

        capture_argv, out_dir = self._capture_argv(
            {"digest": "d1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SanitizerFailure):
                driver.measure(capture_argv=capture_argv, out_dir=out_dir)
        printed = json.loads(buffer.getvalue().strip())
        self.assertIn("amendment_ratio", printed)

    def test_open_campaign_prints_amendment_ratio_before_refusing_a_resumable_predecessor(self) -> None:
        predecessor_run_root = self.root / "predecessor-runs"
        attempt = predecessor_run_root / "attempt-1"
        attempt.mkdir(parents=True)
        (attempt / "manifest.json").write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
        driver = self.driver()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(PredecessorResumableError):
                self.open_campaign(
                    driver,
                    predecessor_run_root=predecessor_run_root,
                    predecessor_graph_id="attempt-1",
                )
        printed = json.loads(buffer.getvalue().strip())
        self.assertIn("amendment_ratio", printed)


class CommandLineTests(_RepoFixture):
    """Both ``AC-CC04-5``'s ``--termination-file`` and ``AC-CC04-7``'s
    top-level ``--repository`` are reachable through the shipped CLI, not
    only the Python API."""

    def test_close_termination_file_reaches_success_via_the_cli(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.ingest(audit_result=_audit("d1"))

        run_result_path = self.root / "run-result.json"
        run_result_path.write_text(
            json.dumps({"status": "succeeded", "candidate_commit": "a" * 40}), encoding="utf-8",
        )
        termination_path = self.root / "termination.json"
        termination_path.write_text(json.dumps({"inspector_recall": 1.0}), encoding="utf-8")

        exit_code = main(
            [
                "--campaign-root", str(self.campaign_root), "--campaign-id", "camp-1",
                "close", "--run-result-file", str(run_result_path),
                "--termination-file", str(termination_path),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "succeeded",
        )

    def test_top_level_repository_flag_reaches_staleness_from_a_step_subcommand(self) -> None:
        (self.repository / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        base_commit = self._git("rev-parse", "HEAD")

        driver = self.driver()
        self.open_campaign(driver, base_commit=base_commit)

        (self.repository / "drift.txt").write_text("drift\n", encoding="utf-8")
        self._git("add", "drift.txt")
        self._git("commit", "-m", "drift")

        exit_code = main(
            [
                "--campaign-root", str(self.campaign_root), "--campaign-id", "camp-1",
                "--repository", str(self.repository), "state",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_measure_subcommand_reads_the_receipt_from_out_dir_via_the_cli(self) -> None:
        self.open_campaign(self.driver())
        # A positional-argument capture command (no embedded "--flags"),
        # since --capture-argv's argparse nargs="+" would otherwise
        # misinterpret a flag inside the captured argv as terminating the
        # list -- a pre-existing property of the CLI's own argument
        # parsing, not something this test is checking.
        capture_argv, out_dir = self._capture_argv(
            {"digest": "cli-1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )

        exit_code = main(
            [
                "--campaign-root", str(self.campaign_root), "--campaign-id", "camp-1",
                "measure", "--capture-argv", *capture_argv, "--out-dir", str(out_dir),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "measured",
        )

    def test_approve_prepare_criteria_texts_by_run_file_reaches_the_check_via_the_cli(self) -> None:
        """``AC-CC04-8``'s criteria byte-identity check is a self-comparison
        unless the caller supplies external packet material to cross-check
        -- and the shipped CLI must actually expose a way to supply it, not
        only the Python API."""

        self.open_campaign(self.driver())
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "plan.md").write_text(
            "Build feature.txt. AC-1: feature works.\n", encoding="utf-8"
        )
        decomposition = {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Build feature.txt. AC-1: feature works."},
            "acceptance_criteria": {"AC-1": "feature works."},
            "runs": [
                {
                    "id": "A", "objective": "Build feature.txt", "plan_sections": ["1"], "criteria": ["AC-1"],
                    "depends_on": [], "allowed_paths": ["feature.txt"],
                    "path_intents": [{"path": "feature.txt", "action": "create"}],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30, "verification_required_paths": [],
                }
            ],
            "functionality_tests": [], "referenced_artifacts": [],
        }
        decomposition_path = self.repository / "decomposition.json"
        decomposition_path.write_text(json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "add plan")

        findings_by_run_path = self.root / "findings-by-run.json"
        findings_by_run_path.write_text(json.dumps({}), encoding="utf-8")
        criteria_texts_path = self.root / "criteria-texts.json"
        criteria_texts_path.write_text(
            json.dumps({"A": [{"id": "AC-1", "text": "this is not what the decomposition says"}]}),
            encoding="utf-8",
        )
        output = self.root / "approval"

        exit_code = main(
            [
                "--campaign-root", str(self.campaign_root), "--campaign-id", "camp-1",
                "approve", "prepare",
                "--repository", str(self.repository), "--decomposition", str(decomposition_path),
                "--output-directory", str(output), "--findings-by-run-file", str(findings_by_run_path),
                "--criteria-texts-by-run-file", str(criteria_texts_path),
            ]
        )
        self.assertEqual(exit_code, 1)
        self.assertFalse(output.exists())


# ---------------------------------------------------------------------------
# AC-CC04-6: stall escalation, regression_suspect ordering
# ---------------------------------------------------------------------------


class StallAndRegressionSuspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = ConvergenceLedger(Path(self.temporary.name) / "ledger.jsonl")

    def test_a_stalled_key_escalates_instead_of_permitting_another_round(self) -> None:
        self.ledger.ingest_audit(_audit("d1", findings=[_finding("a.py", "s1")]))
        self.ledger.record_fix_claimed(("a.py", "s1"))
        self.ledger.ingest_audit(_audit("d2", verdicts=[
            {"key": ["a.py", "s1"], "verdict": "reopened"},
        ]))
        self.ledger.record_fix_claimed(("a.py", "s1"))
        self.ledger.ingest_audit(_audit("d3", verdicts=[
            {"key": ["a.py", "s1"], "verdict": "reopened"},
        ]))
        self.assertTrue(self.ledger.is_stalled())
        budget = RepairRoundBudget(max_repair_rounds=3, repair_rounds_used=0)
        with self.assertRaises(StallEscalation):
            guard_before_plan(ledger=self.ledger, budget=budget)

    def test_tag_regression_suspects_matches_prior_repair_grants(self) -> None:
        newly_opened = [_finding("src/a.py", "new-issue"), _finding("src/other.py", "unrelated")]
        suspects = tag_regression_suspects(
            newly_opened_findings=newly_opened, prior_repair_grants=["src/a.py"],
        )
        self.assertEqual(suspects, (("src/a.py", "new-issue"),))

    def test_a_regression_suspect_never_stalls_on_its_own(self) -> None:
        self.ledger.ingest_audit(
            _audit("d1", findings=[_finding("src/a.py", "regression-candidate")])
        )
        self.assertNotIn(("src/a.py", "regression-candidate"), self.ledger.stalled_keys())
        self.assertFalse(self.ledger.is_stalled())


class DriverStallAndRegressionSuspectTests(_RepoFixture):
    def test_plan_raises_stall_escalation_before_the_round_bound(self) -> None:
        driver = self.driver(max_repair_rounds=3)
        self.open_campaign(driver)
        driver.ledger.ingest_audit(_audit("d1", findings=[_finding("a.py", "s1")]))
        driver.ledger.record_fix_claimed(("a.py", "s1"))
        driver.ledger.ingest_audit(_audit("d2", verdicts=[{"key": ["a.py", "s1"], "verdict": "reopened"}]))
        driver.ledger.record_fix_claimed(("a.py", "s1"))
        driver.ledger.ingest_audit(_audit("d3", verdicts=[{"key": ["a.py", "s1"], "verdict": "reopened"}]))
        with self.assertRaises(StallEscalation):
            driver.plan(
                decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["x.py"]}]},
                findings_by_run={"solo": []},
            )

    def test_ingest_tags_regression_suspects_and_rule_blocks_until_answered(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.plan(
            decomposition={
                "runs": [
                    {"id": "repair-a", "depends_on": [], "allowed_paths": ["src/a.py"]},
                    {"id": "join", "depends_on": ["repair-a"], "allowed_paths": ["src/a.py"]},
                ]
            },
            findings_by_run={"repair-a": [_finding("src/a.py", "first")]},
        )
        result = driver.ingest(
            audit_result=_audit("d1", findings=[_finding("src/a.py", "regression-candidate")])
        )
        self.assertEqual(result["regression_suspect_keys"], (("src/a.py", "regression-candidate"),))

        with self.assertRaises(ConvergenceCampaignDriverError):
            driver.rule(dispositions=[])

        driver.rule(
            dispositions=[
                {
                    "key": ["src/a.py", "regression-candidate"],
                    "disposition": "require_repair",
                    "statement": "confirmed regression; repair required",
                }
            ]
        )
        self.assertNotIn(
            ("src/a.py", "regression-candidate"), driver.ledger.stalled_keys(),
        )


class DriverTargetAmendedWithoutScopeTests(_RepoFixture):
    """``bounds-termination``'s "target amended without scope" blocked end
    state is one ``ConvergenceLedger.is_blocked()`` already derives -- the
    driver must actually consult it, not just the sanitizer/stall/round
    -bound end states."""

    def test_plan_raises_before_the_stall_and_round_bound_checks(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.ledger.record_target_amendment(digest="d1", invalidation_scope=None)

        with self.assertRaises(TargetAmendedWithoutScopeError):
            driver.plan(
                decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["x.py"]}]},
                findings_by_run={"solo": []},
            )
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "blocked",
        )

    def test_plan_prints_amendment_ratio_before_blocking_on_a_target_amendment(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.ledger.record_target_amendment(digest="d1", invalidation_scope=None)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(TargetAmendedWithoutScopeError):
                driver.plan(
                    decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["x.py"]}]},
                    findings_by_run={"solo": []},
                )
        printed = json.loads(buffer.getvalue().strip())
        self.assertIn("amendment_ratio", printed)

    def test_a_later_scoped_amendment_unblocks_the_plan_step(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.ledger.record_target_amendment(digest="d1", invalidation_scope=None)
        driver.ledger.record_target_amendment(digest="d2", invalidation_scope=[])

        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["x.py"]}]},
            findings_by_run={"solo": []},
        )
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "planned",
        )


# ---------------------------------------------------------------------------
# AC-CC04-7: resume reconstructs state from the checkpoint at every step
# ---------------------------------------------------------------------------


class ResumeFromCheckpointTests(_RepoFixture):
    def _fresh(self) -> ConvergenceCampaignDriver:
        return self.driver()

    def test_state_round_trips_through_the_whole_machine(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        self.assertEqual(self._fresh().state()["round"], 0)
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "opened",
        )

        capture_argv, out_dir = self._capture_argv(
            {"digest": "d1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )
        measured = driver.measure(capture_argv=capture_argv, out_dir=out_dir)
        self.assertEqual(self._checkpoint_lifecycle(), "measured")
        self.assertEqual(self._fresh().state()["pending_audit_digest"], measured["digest"])

        driver.ingest(digest=measured["digest"])
        self.assertEqual(self._checkpoint_lifecycle(), "ingested")
        self.assertIsNone(self._fresh().state()["pending_audit_digest"])

        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["src/a.py"]}]},
            findings_by_run={"solo": []},
        )
        self.assertEqual(self._checkpoint_lifecycle(), "planned")
        resumed_state = self._fresh().state()
        self.assertEqual(resumed_state["round"], 1)
        self.assertEqual(resumed_state["join_regression_node_id"], "solo")

        run_result = {
            "status": "succeeded", "candidate_commit": "a" * 40,
            "status_flags": {"success": True},
        }
        recorded_run = driver.run_graph(
            argv=["true", "--on-block-argv", "[]"], runner=lambda *a, **k: _FakeCompleted(run_result)
        )
        self.assertEqual(recorded_run["status"], "succeeded")
        self.assertEqual(self._checkpoint_lifecycle(), "run_succeeded")
        self.assertEqual(self._fresh().state()["last_run_result"]["status"], "succeeded")

        closed = driver.close(run_result=run_result)
        self.assertEqual(self._checkpoint_lifecycle(), "closed")
        self.assertTrue(closed["base_adopted"])
        self.assertEqual(self._fresh().state()["current_base_commit"], "a" * 40)

    def test_rule_step_checkpoints_before_and_after(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["src/a.py"]}]},
            findings_by_run={"solo": [_finding("src/a.py", "seed")]},
        )
        driver.ingest(audit_result=_audit("d1", findings=[_finding("src/a.py", "seed")]))
        driver.rule(dispositions=[])
        self.assertEqual(self._checkpoint_lifecycle(), "ruled")

    def test_approve_prepare_and_issue_checkpoint(self) -> None:
        driver = self.driver()
        self.open_campaign(driver)
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "plan.md").write_text(
            "Build feature.txt. AC-1: feature works.\n", encoding="utf-8"
        )
        decomposition = {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Build feature.txt. AC-1: feature works."},
            "acceptance_criteria": {"AC-1": "feature works."},
            "runs": [
                {
                    "id": "A", "objective": "Build feature.txt", "plan_sections": ["1"], "criteria": ["AC-1"],
                    "depends_on": [], "allowed_paths": ["feature.txt"],
                    "path_intents": [{"path": "feature.txt", "action": "create"}],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30, "verification_required_paths": [],
                }
            ],
            "functionality_tests": [], "referenced_artifacts": [],
        }
        decomposition_path = self.repository / "decomposition.json"
        decomposition_path.write_text(json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "add plan")
        output = self.root / "approval"

        packet = driver.approve_prepare(
            repository=self.repository, decomposition_path=decomposition_path,
            output_directory=output, findings_by_run={"A": [_finding("feature.txt", "missing")]},
        )
        self.assertEqual(self._checkpoint_lifecycle(), "approving")

        from harness_labs.plangraph.plan_approval import OPERATOR_APPROVAL_PROTOCOL

        subject = json.loads(packet.subject_path.read_text(encoding="utf-8"))
        operator_path = output / "operator-approval.json"
        operator_path.write_text(
            json.dumps(
                {
                    "protocol": OPERATOR_APPROVAL_PROTOCOL,
                    "subject_sha256": __import__(
                        "harness_labs.plangraph.plan_graph_contract", fromlist=["sha256_json"]
                    ).sha256_json(subject),
                    "actor": "operator", "approved_at": "2026-08-19T00:00:00Z",
                    "statement": "approved",
                }
            ),
            encoding="utf-8",
        )
        driver.approve_issue(
            repository=self.repository, subject_path=packet.subject_path,
            gate_evidence_path=packet.gate_evidence_path, operator_approval_path=operator_path,
            receipt_path=output / "receipt.json",
        )
        self.assertEqual(self._checkpoint_lifecycle(), "approved")

    def _checkpoint_lifecycle(self) -> str:
        return json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"]


class CheckpointStalenessReachableFromStepMachineTests(_RepoFixture):
    """``AC-CC04-7``: a driver constructed with ``repository=`` requests
    checkpoint staleness verification on every internal ``state()`` call
    across the step machine (measure/ingest/rule/plan/...), not only the
    standalone ``state`` CLI diagnostic."""

    def _seed_commit(self) -> str:
        (self.repository / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        return self._git("rev-parse", "HEAD")

    def test_a_commit_between_two_steps_is_detected_as_staleness(self) -> None:
        from harness_labs.plangraph.convergence_campaign import CampaignCheckpointStaleError

        base_commit = self._seed_commit()
        driver = self.driver(repository=self.repository)
        self.open_campaign(driver, base_commit=base_commit)
        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["src/a.py"]}]},
            findings_by_run={"solo": []},
        )

        # A commit lands on the repository between two steps.
        (self.repository / "drift.txt").write_text("drift\n", encoding="utf-8")
        self._git("add", "drift.txt")
        self._git("commit", "-m", "drift")

        capture_argv, out_dir = self._capture_argv(
            {"digest": "d1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )
        with self.assertRaises(CampaignCheckpointStaleError):
            driver.measure(capture_argv=capture_argv, out_dir=out_dir)

    def test_without_a_configured_repository_staleness_is_never_checked(self) -> None:
        base_commit = self._seed_commit()
        driver = self.driver()  # no repository= -- unchanged, backward-compatible behavior
        self.open_campaign(driver, base_commit=base_commit)
        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["src/a.py"]}]},
            findings_by_run={"solo": []},
        )
        (self.repository / "drift.txt").write_text("drift\n", encoding="utf-8")
        self._git("add", "drift.txt")
        self._git("commit", "-m", "drift")

        # No repository was configured on the driver, so nothing checks the
        # base_commit against the (now different) repository head.
        capture_argv, out_dir = self._capture_argv(
            {"digest": "d1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )
        measured = driver.measure(capture_argv=capture_argv, out_dir=out_dir)
        self.assertEqual(len(measured["digest"]), 64)


class BaseAdoptionAutomaticMeasureWithRepositoryTests(_RepoFixture):
    """A base adoption moves ``current_base_commit`` to a candidate commit
    that is never the live-checked-out repository worktree head (candidates
    are commit-tree objects, not a worktree checkout). The automatic
    post-repair ``measure`` chained from ``close`` must still run against a
    driver configured with ``repository=`` -- it must not misread the
    just-adopted base as external staleness."""

    def test_close_with_repository_configured_adopts_a_base_and_still_auto_measures(self) -> None:
        (self.repository / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        base_commit = self._git("rev-parse", "HEAD")

        driver = self.driver(repository=self.repository)
        self.open_campaign(driver, base_commit=base_commit)
        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["src/a.py"]}]},
            findings_by_run={"solo": []},
        )

        # The round's joined candidate: a real commit-tree object the
        # worktree is never checked out to, exactly as PlanGraph produces
        # one (never a "git checkout"/"git reset --hard").
        candidate_commit = self._git(
            "commit-tree", self._git("rev-parse", "HEAD^{tree}"), "-p", base_commit,
            "-m", "round candidate",
        )
        self.assertNotEqual(candidate_commit, self._git("rev-parse", "HEAD"))

        capture_argv, out_dir = self._capture_argv(
            {"digest": "d1", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )

        closed = driver.close(
            run_result={"status": "succeeded", "candidate_commit": candidate_commit},
            capture_argv=capture_argv, out_dir=out_dir,
        )

        self.assertTrue(closed["base_adopted"])
        self.assertEqual(closed["new_base_commit"], candidate_commit)
        self.assertIsNotNone(closed["measure_result"])
        self.assertEqual(closed["measure_result"]["audit_result"]["digest"], "d1")
        self.assertEqual(
            json.loads((self.campaign_root / "checkpoint.json").read_text(encoding="utf-8"))["lifecycle"],
            "measured",
        )

    def test_a_separately_invoked_measure_after_close_still_gets_full_staleness_checking(self) -> None:
        """The ``_state`` bypass is close()'s own internal chaining only: a
        *separately invoked* ``measure`` call (a new step, e.g. from the
        CLI) still performs full live-head staleness verification, and
        catches genuine external drift the same as before."""

        from harness_labs.plangraph.convergence_campaign import CampaignCheckpointStaleError

        (self.repository / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        base_commit = self._git("rev-parse", "HEAD")

        driver = self.driver(repository=self.repository)
        self.open_campaign(driver, base_commit=base_commit)
        driver.plan(
            decomposition={"runs": [{"id": "solo", "depends_on": [], "allowed_paths": ["src/a.py"]}]},
            findings_by_run={"solo": []},
        )
        candidate_commit = self._git(
            "commit-tree", self._git("rev-parse", "HEAD^{tree}"), "-p", base_commit,
            "-m", "round candidate",
        )

        driver.close(run_result={"status": "succeeded", "candidate_commit": candidate_commit})

        # The live worktree is still at base_commit (nothing ever checked it
        # out to candidate_commit); a fresh, separately invoked measure call
        # must still detect that mismatch as staleness.
        capture_argv, out_dir = self._capture_argv(
            {"digest": "d2", "findings": [], "verdicts": [], "confirmed_good": [], "capture_coverage": {}}
        )
        with self.assertRaises(CampaignCheckpointStaleError):
            driver.measure(capture_argv=capture_argv, out_dir=out_dir)


class _FakeCompleted:
    def __init__(self, payload: dict) -> None:
        self.stdout = json.dumps(payload)
        self.returncode = 0
        self.stderr = ""


# ---------------------------------------------------------------------------
# Findings-owners-paths table rendering (used by AC-CC04-1)
# ---------------------------------------------------------------------------


class FindingsOwnersPathsTableTests(unittest.TestCase):
    def test_one_row_per_owner_finding_sorted_stably(self) -> None:
        table = render_findings_owners_paths_table(
            {
                "B": [_finding("b.py", "b-issue")],
                "A": [_finding("a.py", "a-issue"), _finding("a.py", "a-issue-2")],
            }
        )
        self.assertEqual([row["run_id"] for row in table], ["A", "A", "B"])
        self.assertEqual(table[0]["file"], "a.py")


if __name__ == "__main__":
    unittest.main()
