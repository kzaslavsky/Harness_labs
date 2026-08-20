"""CC-05 lifecycle proof (``build-order-cc-05``, ``tests-lifecycle``).

One subprocess-level run of the full convergence-campaign slice, through the
shipped CLIs, on a static fixture:

  capture -> keyed inspection -> findings ingest -> rule -> one approved
  repair graph run via ``scripts/run_plan_graph.py`` with the deterministic
  scripted launcher fixture (``tests/fixtures/convergence_lifecycle_launcher.py``)
  -> deterministic join -> post-repair capture -> per-key verdicts closing
  the seeded finding.

Three CLIs run as real subprocesses:

* ``scripts/ui_fidelity_capture.py`` (the CC-03 measurer), invoked via
  ``ConvergenceCampaignDriver.measure``'s ``capture_argv`` -- both rounds.
  ``measure`` itself stays a direct Python-API call: what gets sealed and
  later ingested is the receipt the driver resolves from the real capture
  subprocess's own ``<out_dir>/receipt.json``, never a hand-run capture or a
  hand-sealed receipt. ``scripts/run_convergence_campaign.py``'s own
  ``measure`` subcommand cannot carry this real invocation -- its
  ``--capture-argv`` (``nargs="+"``) stops consuming at the first
  ``-``-prefixed token, which ``ui_fidelity_capture.py``'s own
  ``--app-dir``/``--matrix``/``--out``/``--driver`` flags are (see
  ``tests/test_convergence_campaign_driver.py``'s own ``_capture_argv``
  helper, which routes around this by writing a positional-only fake
  capture command -- not an option open to a test that must run the real
  capture script).
* ``scripts/run_plan_graph.py`` (the CC-05 repair graph), invoked via
  ``ConvergenceCampaignDriver.run_graph``'s ``argv``.
* ``scripts/run_convergence_campaign.py`` itself, invoked via
  ``subprocess.run`` (see ``_campaign_cli`` below) for every other step of
  the measure/ingest/rule/plan/approve/run/close machine that has a CLI
  subcommand: ingest (both rounds), rule, plan, approve prepare, approve
  issue, and close (invoked twice -- once right after the repair graph
  succeeds to adopt the joined candidate, and again after the post-repair
  ingest to evaluate ``bounds-termination`` via its own
  ``--termination-file``, since that predicate is only meaningful once the
  round's real post-repair verdicts have been folded into the ledger, which
  must happen between the two). ``open_campaign`` has no CLI subcommand at
  all (see ``_parser()`` in ``scripts/run_convergence_campaign.py``) and
  stays a direct Python-API call for that reason alone.

Every subprocess CLI invocation above also carries the campaign CLI's own
top-level ``--repository`` flag, and the Python-API ``ConvergenceCampaignDriver``
used for ``open``/``measure``/``run``/``close`` is likewise constructed with
``repository=`` -- so every checkpoint load across the whole lifecycle
(subprocess and Python-API alike) requests the checkpoint store's staleness
and sequence-regression guards (``CampaignCheckpointStaleError``/
``CampaignCheckpointSequenceError``), not only the CLI's standalone ``state``
diagnostic. Two things make that safe end to end rather than merely
accidental:

1. Admission's own incidental commit (``commit_findings_owners_paths_table``,
   which lands a ``findings-owners-paths.json`` commit on the base branch
   whenever the round's table differs from what is already committed) would
   otherwise silently advance the repository head past the checkpoint's
   recorded ``current_base_commit`` with nothing in the driver updating that
   field to match -- the very next checkpoint load would then see genuine
   (self-inflicted) staleness. ``_scaffold_repository`` below pre-seeds the
   fixture-repo commit with exactly the table admission would otherwise
   produce (``render_findings_owners_paths_table`` only reads a finding's
   ``file``/``subject``/``required_paths``, all of which this fixture's
   findings are deterministic in, independent of a real capture's computed
   color), so admission's own call finds nothing to commit.
2. A base adoption's candidate is a commit-tree object the repository
   worktree is never automatically checked out to (``close``'s own
   docstring: candidates are "never a worktree checkout"). This test
   performs that checkout itself, but strictly *after* the first ``close``
   call that adopts the base (so that call's own staleness check still
   compares against the pre-adoption head) and *before* the separately
   invoked post-repair ``measure`` (so its staleness check compares against
   the now-current, just-adopted head) -- exactly the ordering
   ``tests/test_convergence_campaign_driver.py``'s own
   ``BaseAdoptionAutomaticMeasureWithRepositoryTests`` establishes as
   correct for a ``repository=``-configured driver.

The repair graph is a real two-repair-node-plus-join shape (S9: fan-in <= 3,
only the join/regression node exceeds it): ``repair-index-title-color`` and
``repair-about-title-color`` are file-disjoint siblings with no dependency
ordering between them, and ``join-and-regression`` depends on both.
``scripts/run_plan_graph.py`` exposes no ``--max-parallelism`` flag, so the
``PlanGraph`` it builds always runs its default sequential dispatch path,
which composes the two independent repair nodes by chaining base commits
(the second-dispatched sibling's base is the first's own sealed candidate,
not the plan's shared base commit) rather than through the git-merge join
path (``PlanGraph._join_candidates``) that only ``max_parallelism > 1``
reaches -- see ``tests/fixtures/convergence_lifecycle_launcher.py``'s module
docstring for the mechanics this test discovered and now asserts on
directly, rather than assuming a merge commit. Either way the base commit
each node receives, and the join node's own regression check, are entirely
PlanGraph's decision; the launcher fixture never reimplements or second-
guesses it -- its only job is the FeatureRun launcher seat: deterministically
produce each node's own candidate commit and return a ``FeatureRunOutcome``
mapping. That the join actually happened -- not merely that the graph
"succeeded" -- is verified directly from the repair attempt's own
``run_root/<attempt>/checkpoint.json`` (the same file
``scripts/run_convergence_campaign.py``'s own ``base_adoption_decision``/
``join_node_sealed`` read): each repair node's ``state.nodes[*]`` entry
must itself report ``status == "succeeded"`` with its own ``candidate_commit``
already carrying that node's fix, and the join node's entry must report
``status == "succeeded"`` with ``candidate_commit`` equal to the round's
final candidate -- grounding the "join happened" claim in PlanGraph's own
per-node audit record rather than in ``close``'s own ``join_sealed`` flag
(which is derived from exactly that same status, but never inspected here
before this fix) or in content assertions alone (which would hold
identically under sequential dispatch even had the join node never run).

Human-inputs claim (asserted, not just stated, and checked non-tautologically):
exactly two artifacts are authored on the human's behalf at flow time --
``operator-approval.json`` and the scripted rule dispositions file. Both,
and only those two, are written inside this test's temporary product
repository, under a ``human-inputs/`` directory the fixture scaffold commits
a ``.gitignore`` entry for (so ``check_pristine_worktree``'s own
``git status --porcelain`` -- run again independently by admission, not
this test -- never sees them as untracked). The check at the end of this
test does not merely list that directory's own two writes back at itself:
it also runs ``git status --porcelain --ignored`` over the whole repository
worktree (asserting the only ignored path is ``human-inputs/`` itself and
that no other path is untracked or modified) and ``git rev-list`` over the
tracked branch (asserting its history is exactly the one fixture-scaffold
commit, proving no flow step -- including admission's own table commit,
neutralized above -- ever added a second one). Every other repository file
(the fixture app copy, ``matrix.json``, ``docs/plan.md``,
``decomposition.json``, the repository-identity file, and the pre-seeded
``findings-owners-paths.json``) is fixture-repo scaffolding committed once
before the campaign flow starts, not a human input produced by a flow step.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_labs.core.ui_fidelity_inspector import validate_inspection_result
from harness_labs.plangraph.convergence_campaign import CHECKPOINT_PROTOCOL
from harness_labs.plangraph.convergence_ledger import ConvergenceLedger
from harness_labs.plangraph.plan_graph_contract import sha256_json
from scripts.run_convergence_campaign import (
    ConvergenceCampaignDriver,
    FINDINGS_OWNERS_PATHS_RELATIVE_PATH,
    render_findings_owners_paths_table,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "ui_fidelity_capture.py"
RUN_PLAN_GRAPH_SCRIPT = REPO_ROOT / "scripts" / "run_plan_graph.py"
RUN_CONVERGENCE_CAMPAIGN_SCRIPT = REPO_ROOT / "scripts" / "run_convergence_campaign.py"
FIXTURE_APP = REPO_ROOT / "tests" / "fixtures" / "convergence_fixture_app"
LAUNCHER_REFERENCE = "tests.fixtures.convergence_lifecycle_launcher:launch"

# The wrong-vs-target colors this lifecycle's one seeded delta is about. The
# fixture app's own committed copy already carries the target color; this
# test corrupts its own private copy of it to create a real, observable
# delta the repair round must close.
_WRONG_COLOR_STYLE = 'data-style-light="color:#ff0000;font-size:24px"'
_TARGET_COLOR_STYLE = 'data-style-light="color:#111827;font-size:24px"'
_TARGET_COLOR = "#111827"

_SELECTOR = "#page-title"
_VIEWPORT = {"name": "desktop", "width": 1280, "height": 800}
_THEME = "light"
_INTERACTION = {"name": "none", "steps": []}
_SUBJECT = "page-title-light-color"

# (route, subject file, owning repair node id) -- the one source of truth
# for both the deterministic pre/post inspection sweeps and the
# fixture-scaffold-time findings-owners-paths pre-seed, so the two cannot
# silently drift apart.
_ROUTE_FILES = (
    ("index.html", "app/index.html", "repair-index-title-color"),
    ("about.html", "app/about.html", "repair-about-title-color"),
)

_ACCEPTANCE_CRITERIA = {
    "AC-CC05-REPAIR-INDEX": "app/index.html #page-title renders color:#111827 in light theme.",
    "AC-CC05-REPAIR-ABOUT": "app/about.html #page-title renders color:#111827 in light theme.",
}


def _subprocess_env() -> dict[str, str]:
    """PYTHONPATH pointed at this checkout, matching
    ``tests/test_ui_fidelity_capture.py``'s own convention -- the capture
    script does no ``sys.path`` surgery of its own, unlike
    ``scripts/run_plan_graph.py`` and ``scripts/run_convergence_campaign.py``,
    both of which insert their own parent onto ``sys.path`` and need no such
    env var."""

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{existing}" if existing else str(REPO_ROOT)
    return env


def _cell_id(route: str) -> str:
    return f"{route}|{_VIEWPORT['name']}|{_THEME}|{_INTERACTION['name']}"


def _find_cell(receipt: dict[str, Any], route: str) -> dict[str, Any]:
    return next(cell for cell in receipt["cells"] if cell["cell_id"] == _cell_id(route))


def _artifact_path(receipt: dict[str, Any], cell: dict[str, Any], kind: str) -> Path:
    return Path(receipt["audit_run_dir"]) / cell["artifact_paths"][kind]


class ConvergenceLifecycleTests(unittest.TestCase):
    """AC-CC05-1, AC-CC05-2."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.human_inputs_dir = self.repository / "human-inputs"
        self.approval_dir = self.root / "approval"
        self.campaign_root = self.root / "campaign"
        self.campaign_id = "cc05-lifecycle"
        self._env_overrides: dict[str, str | None] = {}
        self.addCleanup(self._restore_env)

    def _setenv(self, name: str, value: str) -> None:
        self._env_overrides.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    def _restore_env(self) -> None:
        for name, previous in self._env_overrides.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True, capture_output=True, check=True,
        )
        return completed.stdout.strip()

    def _campaign_cli(self, *args: str) -> dict[str, Any]:
        """One real ``scripts/run_convergence_campaign.py`` subprocess,
        carrying the campaign CLI's own top-level ``--repository`` flag so
        this step's checkpoint load requests staleness/sequence-regression
        verification the same as every other step of this lifecycle."""

        completed = subprocess.run(
            [
                sys.executable, str(RUN_CONVERGENCE_CAMPAIGN_SCRIPT),
                "--campaign-root", str(self.campaign_root),
                "--campaign-id", self.campaign_id,
                "--repository", str(self.repository),
                *args,
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(
            completed.returncode, 0,
            f"scripts/run_convergence_campaign.py {list(args)!r} exited "
            f"{completed.returncode}: {completed.stderr}",
        )
        # evaluate_success_termination's own emit=print prints a leading
        # "termination"/amendment-ratio line on any step touching
        # termination (AC-CC04-5: printed at every termination, success or
        # not) before main()'s own final payload line -- the step's real
        # result is always that last line.
        stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        return json.loads(stdout_lines[-1])

    # -- fixture-repo scaffolding (not a human input; committed once, before
    # the campaign flow starts) ---------------------------------------------

    def _expected_findings_by_run(self) -> dict[str, list[dict[str, Any]]]:
        return {
            run_id: [{"file": subject_file, "subject": _SUBJECT, "required_paths": [subject_file]}]
            for _route, subject_file, run_id in _ROUTE_FILES
        }

    def _scaffold_repository(self) -> str:
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "cc05-lifecycle-tests@example.com")
        self._git("config", "user.name", "CC-05 Lifecycle Tests")

        # Human-authored files (dispositions, operator approval) live under
        # this gitignored directory -- untracked, but invisible to
        # ``check_pristine_worktree``'s own ``git status --porcelain``
        # (which does not report ignored paths).
        (self.repository / ".gitignore").write_text("human-inputs/\n", encoding="utf-8")

        harness_dir = self.repository / ".harness"
        harness_dir.mkdir()
        (harness_dir / "repository.json").write_text(
            json.dumps(
                {
                    "protocol": "harness-repository-identity/1",
                    "repository_id": "cc05-lifecycle-test-repository",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        app_dir = self.repository / "app"
        app_dir.mkdir()
        for name in ("index.html", "about.html", "app.js", "style.css"):
            source = (FIXTURE_APP / name).read_text(encoding="utf-8")
            if name in ("index.html", "about.html"):
                self.assertIn(
                    _TARGET_COLOR_STYLE, source,
                    f"fixture {name} no longer declares the expected baseline style",
                )
                source = source.replace(_TARGET_COLOR_STYLE, _WRONG_COLOR_STYLE)
            (app_dir / name).write_text(source, encoding="utf-8")

        docs_dir = self.repository / "docs"
        docs_dir.mkdir()
        (docs_dir / "plan.md").write_text(
            "# CC-05 Lifecycle Repair Plan\n"
            "\n"
            "## Fix page-title color [fix-page-title-color]\n"
            "\n"
            "Repair the #page-title element so its light-theme color matches "
            "the target's declared #111827 on both app/index.html and "
            "app/about.html, each file repaired independently, then "
            "re-verify both together as one regression check.\n",
            encoding="utf-8",
        )

        # Pre-seeded exactly as admission's own ``commit_findings_owners_paths_table``
        # would render it from this round's real findings (see the module
        # docstring): admission's later call then finds no diff and commits
        # nothing, so the repository head this test's ``repository=``-configured
        # driver tracks never drifts out from under the checkpoint's own
        # recorded ``current_base_commit`` during approval.
        findings_table = render_findings_owners_paths_table(self._expected_findings_by_run())
        (self.repository / FINDINGS_OWNERS_PATHS_RELATIVE_PATH).write_text(
            json.dumps([dict(row) for row in findings_table], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        decomposition = self._build_decomposition()
        (self.repository / "decomposition.json").write_text(
            json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8",
        )

        self._git("add", "-A")
        self._git("commit", "-m", "cc05 lifecycle fixture scaffold")
        return self._git("rev-parse", "HEAD")

    def _matrix(self, *, routes: list[str]) -> dict[str, Any]:
        return {
            "routes": routes,
            "viewports": [_VIEWPORT],
            "themes": [_THEME],
            "interactions": [_INTERACTION],
            "selectors": [_SELECTOR],
        }

    def _repair_objective(self) -> str:
        return (
            "Repair the #page-title element so its light-theme color matches "
            "the target's declared #111827 on both app/index.html and "
            "app/about.html"
        )

    def _regression_check_argv(self) -> list[str]:
        script = (
            "import pathlib\n"
            "for p in ('app/index.html', 'app/about.html'):\n"
            "    text = pathlib.Path(p).read_text(encoding='utf-8')\n"
            f"    assert {_TARGET_COLOR_STYLE!r} in text, p\n"
        )
        return [sys.executable, "-c", script]

    def _node_check_argv(self, path: str) -> list[str]:
        script = (
            "import pathlib\n"
            f"text = pathlib.Path({path!r}).read_text(encoding='utf-8')\n"
            f"assert {_TARGET_COLOR_STYLE!r} in text\n"
        )
        return [sys.executable, "-c", script]

    def _criteria_texts_by_run(self) -> dict[str, list[dict[str, str]]]:
        """External packet material genuinely distinct from the
        decomposition's own ``runs[*]["criteria"]``/``acceptance_criteria``
        self-derivation ``check_criteria_byte_identity`` falls back to when
        this is omitted (a tautology by that function's own docstring): a
        real cross-check that would catch a decomposition whose
        acceptance-criteria text drifted from what an approval packet
        elsewhere quotes, sourced from the same ``_ACCEPTANCE_CRITERIA``
        constant ``_build_decomposition`` uses, but supplied through the
        shipped CLI's own ``--criteria-texts-by-run-file``."""

        return {
            "repair-index-title-color": [
                {"id": "AC-CC05-REPAIR-INDEX", "text": _ACCEPTANCE_CRITERIA["AC-CC05-REPAIR-INDEX"]},
            ],
            "repair-about-title-color": [
                {"id": "AC-CC05-REPAIR-ABOUT", "text": _ACCEPTANCE_CRITERIA["AC-CC05-REPAIR-ABOUT"]},
            ],
        }

    def _build_decomposition(self) -> dict[str, Any]:
        objective = self._repair_objective()
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {
                "fix-page-title-color": "## Fix page-title color [fix-page-title-color]",
            },
            "acceptance_criteria": dict(_ACCEPTANCE_CRITERIA),
            "runs": [
                {
                    "id": "repair-index-title-color",
                    "objective": objective,
                    "plan_sections": ["fix-page-title-color"],
                    "criteria": ["AC-CC05-REPAIR-INDEX"],
                    "depends_on": [],
                    "allowed_paths": ["app/index.html"],
                    "path_intents": [{"path": "app/index.html", "action": "modify"}],
                    "verification_argv": self._node_check_argv("app/index.html"),
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
                {
                    "id": "repair-about-title-color",
                    "objective": objective,
                    "plan_sections": ["fix-page-title-color"],
                    "criteria": ["AC-CC05-REPAIR-ABOUT"],
                    "depends_on": [],
                    "allowed_paths": ["app/about.html"],
                    "path_intents": [{"path": "app/about.html", "action": "modify"}],
                    "verification_argv": self._node_check_argv("app/about.html"),
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
                {
                    "id": "join-and-regression",
                    "objective": objective,
                    "plan_sections": ["fix-page-title-color"],
                    "criteria": ["AC-CC05-REPAIR-INDEX", "AC-CC05-REPAIR-ABOUT"],
                    "depends_on": ["repair-index-title-color", "repair-about-title-color"],
                    "allowed_paths": ["app/index.html", "app/about.html"],
                    "path_intents": [
                        {"path": "app/index.html", "action": "modify"},
                        {"path": "app/about.html", "action": "modify"},
                    ],
                    "verification_argv": self._regression_check_argv(),
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
            ],
            "functionality_tests": [],
            "referenced_artifacts": [FINDINGS_OWNERS_PATHS_RELATIVE_PATH],
        }

    # -- keyed inspection (the CC-03 inspector role; a plain function here,
    # since the plan defines inspection as a judgment role, not a shipped
    # CLI -- ``measurer-requirements``). Both sweeps validate their own
    # output through the shipped inspector-role validator
    # (``harness_labs.core.ui_fidelity_inspector.validate_inspection_result``)
    # against the ledger's own live open-key set, rather than hand-rolling
    # per-key verdict completeness -- so a worker silently dropping a prior
    # key is caught here, not laundered into an implicit "unobserved" at
    # ingest. --------------------------------------------------------------

    def _inspect_pre_repair(self, receipt: dict[str, Any], driver: ConvergenceCampaignDriver) -> dict[str, Any]:
        findings = []
        for route, subject_file, _run_id in _ROUTE_FILES:
            cell = _find_cell(receipt, route)
            self.assertEqual(cell["status"], "ok", f"capture cell for {route} was not stable")
            computed = json.loads(_artifact_path(receipt, cell, "computed_styles").read_text())
            actual_color = computed.get(_SELECTOR, {}).get("color")
            self.assertNotEqual(
                actual_color, _TARGET_COLOR,
                f"fixture setup did not seed a wrong color for {route}",
            )
            dom_ref = cell["artifacts"]["dom_snapshot"]
            findings.append(
                {
                    "file": subject_file,
                    "subject": _SUBJECT,
                    "required_paths": [subject_file],
                    "confidence": "C",
                    "supersedes_key": None,
                    "statement": (
                        f"#page-title computed color is {actual_color!r} in the light "
                        f"theme on {route}; target requires {_TARGET_COLOR!r}."
                    ),
                    "category": "ui-fidelity",
                    "severity": "major",
                    "requires_disposition": False,
                    "evidence_refs": [dom_ref],
                }
            )
        coverage = {cell["cell_id"]: cell["status"] for cell in receipt["cells"]}
        payload = {
            "findings": findings,
            "verdicts": [],
            "confirmed_good": [],
            "capture_coverage": coverage,
        }
        # No key was ever opened before round 1, so this is a real (if
        # trivially empty) call: it establishes that every inspection sweep
        # in this lifecycle -- not only the post-repair one -- runs through
        # the shipped validator.
        validate_inspection_result(payload, prior_keys=driver.ledger.open_set())
        return {"digest": sha256_json(payload), **payload}

    def _inspect_post_repair(self, receipt: dict[str, Any], driver: ConvergenceCampaignDriver) -> dict[str, Any]:
        verdicts = []
        for route, subject_file, _run_id in _ROUTE_FILES:
            cell = _find_cell(receipt, route)
            self.assertEqual(cell["status"], "ok", f"post-repair capture cell for {route} was not stable")
            computed = json.loads(_artifact_path(receipt, cell, "computed_styles").read_text())
            actual_color = computed.get(_SELECTOR, {}).get("color")
            verdict_kind = "observed_fixed" if actual_color == _TARGET_COLOR else "reopened"
            entry: dict[str, Any] = {
                "key": [subject_file, _SUBJECT],
                "verdict": verdict_kind,
            }
            if verdict_kind == "observed_fixed":
                entry["capture_cell"] = cell["cell_id"]
                entry["assertion"] = (
                    f"#page-title computed color == {_TARGET_COLOR!r} in the light "
                    f"theme on {route} (post-repair capture)"
                )
            verdicts.append(entry)
        coverage = {cell["cell_id"]: cell["status"] for cell in receipt["cells"]}
        payload = {
            "findings": [],
            "verdicts": verdicts,
            "confirmed_good": [],
            "capture_coverage": coverage,
        }
        prior_keys = driver.ledger.open_set()
        validated = validate_inspection_result(payload, prior_keys=prior_keys)
        self.assertEqual(
            set(validated), set(prior_keys),
            "the post-repair inspection must carry an explicit verdict for "
            "every key the ledger currently has open, and nothing else",
        )
        return {"digest": sha256_json(payload), **payload}

    # -- the lifecycle run ----------------------------------------------------

    def test_full_lifecycle_closes_the_seeded_finding(self) -> None:
        base_commit = self._scaffold_repository()
        self.human_inputs_dir.mkdir()

        driver = ConvergenceCampaignDriver(
            campaign_root=self.campaign_root, campaign_id=self.campaign_id,
            repository=self.repository,
        )

        # -- open (no shipped CLI subcommand exists for this step; see the
        # module docstring) ---------------------------------------------------
        target_path = self.root / "target.md"
        target_path.write_text(
            "# UI Fidelity Target\n\n"
            "The #page-title heading must render color: #111827 in the "
            "light theme on every route.\n",
            encoding="utf-8",
        )
        driver.open_campaign(
            domain="ui-fidelity",
            source_path=target_path,
            target_kind="design-doc",
            snapshot_relative_path="target.md",
            base_commit=base_commit,
            pre_journal_sanitizer="scripts.run_convergence_campaign:identity_pre_journal_sanitizer",
            recall_threshold=0.0,
            amendment_ratio_threshold=1.0,
            # DTR-MC's campaign-open commissioning checklist
            # (build_campaign_config) refuses a config lacking
            # stability_report_digest/recall_report_digest absent an
            # explicit, reasoned override; this lifecycle fixture exercises
            # the real measure/ingest/rule/plan/approve/run/close machine,
            # not measurer commissioning itself, so it carries the override.
            commissioning_override={
                "reason": "CC-05 lifecycle proof; commissioning artifacts not exercised here",
            },
        )

        # -- round 1: measure (real subprocess: scripts/ui_fidelity_capture.py,
        # via the driver's own Python API -- see the module docstring for why
        # this step in particular cannot go through the campaign CLI's own
        # measure subcommand). --------------------------------------------
        matrix_path = self.root / "matrix.json"
        matrix_path.write_text(
            json.dumps(self._matrix(routes=["index.html", "about.html"])), encoding="utf-8",
        )
        capture_out_1 = self.root / "capture-round-1"
        capture_argv_1 = [
            sys.executable, str(CAPTURE_SCRIPT),
            "--app-dir", str(self.repository / "app"),
            "--matrix", str(matrix_path),
            "--out", str(capture_out_1),
            "--driver", "stub",
        ]
        measured_1 = driver.measure(
            capture_argv=capture_argv_1, out_dir=capture_out_1,
            runner=functools.partial(subprocess.run, env=_subprocess_env()),
        )
        receipt_1 = measured_1["audit_result"]
        self.assertEqual(receipt_1["exit_code"], 0)
        self.assertEqual(receipt_1["driver"]["kind"], "stub")

        # -- keyed inspection over the just-captured evidence -----------------
        audit_result_1 = self._inspect_pre_repair(receipt_1, driver)
        evidence_sources_1 = {}
        for route, _subject_file, _run_id in _ROUTE_FILES:
            cell = _find_cell(receipt_1, route)
            evidence_sources_1[cell["artifacts"]["dom_snapshot"]] = _artifact_path(
                receipt_1, cell, "dom_snapshot",
            )
        driver.artifacts.seal_audit_result(audit_result_1, evidence_sources=evidence_sources_1)

        # -- findings ingest (real subprocess: scripts/run_convergence_campaign.py
        # ingest) ---------------------------------------------------------------
        audit_result_1_path = self.root / "audit-result-1.json"
        audit_result_1_path.write_text(json.dumps(audit_result_1), encoding="utf-8")
        ingest_1 = self._campaign_cli("ingest", "--audit-result-file", str(audit_result_1_path))
        self.assertEqual(len(ingest_1["summary"]["opened"]), 2)

        # -- rule: the human's scripted disposition input, read via the real
        # ``rule`` subcommand. No key was tagged regression_suspect this
        # round (prior_repair_grants is empty on round 1), so the real,
        # authored disposition set is empty -- an explicit "nothing to rule
        # on" is still a human decision, not a placeholder. -------------------
        dispositions_path = self.human_inputs_dir / "dispositions-round-1.json"
        dispositions_path.write_text(json.dumps([]) + "\n", encoding="utf-8")
        self._campaign_cli("rule", "--dispositions-file", str(dispositions_path))

        # -- plan: register the round's repair decomposition (already
        # committed as part of fixture-repo scaffolding above; the plan step
        # here is campaign-driver bookkeeping over that committed shape, not
        # a second human-authored file), via the real ``plan`` subcommand. --
        decomposition_path = self.repository / "decomposition.json"
        findings_by_run = {
            "repair-index-title-color": [
                f for f in audit_result_1["findings"] if f["file"] == "app/index.html"
            ],
            "repair-about-title-color": [
                f for f in audit_result_1["findings"] if f["file"] == "app/about.html"
            ],
        }
        findings_by_run_path = self.root / "findings-by-run.json"
        findings_by_run_path.write_text(json.dumps(findings_by_run), encoding="utf-8")
        plan_result = self._campaign_cli(
            "plan", "--decomposition", str(decomposition_path),
            "--findings-by-run-file", str(findings_by_run_path),
        )
        self.assertEqual(plan_result["join_regression_node_id"], "join-and-regression")

        # -- approve: prepare (admission, real ``approve prepare`` subcommand,
        # with the run-owned criteria quotes supplied via
        # --criteria-texts-by-run-file so AC-CC04-8's byte-identity check runs
        # against real external packet material rather than the tautological
        # decomposition-self-derived default) then issue (the human's
        # approval file -- the second, and last, human input; real
        # ``approve issue`` subcommand). ---------------------------------
        criteria_texts_path = self.root / "criteria-texts-by-run.json"
        criteria_texts_path.write_text(json.dumps(self._criteria_texts_by_run()), encoding="utf-8")
        approve_prepare_result = self._campaign_cli(
            "approve", "prepare",
            "--repository", str(self.repository),
            "--decomposition", str(decomposition_path),
            "--output-directory", str(self.approval_dir),
            "--findings-by-run-file", str(findings_by_run_path),
            "--criteria-texts-by-run-file", str(criteria_texts_path),
        )
        self.assertEqual(
            approve_prepare_result["warnings"], [],
            "the repair decomposition was engineered to declare complete "
            "path intents so admission emits no warnings; if this fails the "
            "flow's operator-approval.json would need to acknowledge them, "
            "which this test does not attempt",
        )

        subject_path = Path(approve_prepare_result["subject"])
        gate_evidence_path = Path(approve_prepare_result["gate_evidence"])
        subject = json.loads(subject_path.read_text())
        operator_approval_path = self.human_inputs_dir / "operator-approval.json"
        operator_approval_path.write_text(
            json.dumps(
                {
                    "protocol": "plan-operator-approval/1",
                    "subject_sha256": sha256_json(subject),
                    "actor": "cc05-lifecycle-test-operator",
                    "approved_at": "2026-01-01T00:00:00Z",
                    "statement": (
                        "Approved: page-title light-theme color repair on "
                        "app/index.html and app/about.html, verified by the "
                        "join/regression node's own check."
                    ),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        receipt_path = self.approval_dir / "receipt.json"
        self._campaign_cli(
            "approve", "issue",
            "--repository", str(self.repository),
            "--subject", str(subject_path),
            "--gate-evidence", str(gate_evidence_path),
            "--operator-approval", str(operator_approval_path),
            "--receipt", str(receipt_path),
        )

        # -- run: the repair graph, via the real scripts/run_plan_graph.py
        # subprocess and the deterministic scripted launcher fixture. -------
        self._setenv("CONVERGENCE_LIFECYCLE_REPOSITORY", str(self.repository))
        run_root = self.root / "plan-graph-runs"
        graph_attempt_id = "cc05-repair-round-1"
        run_argv = [
            sys.executable, str(RUN_PLAN_GRAPH_SCRIPT),
            "run",
            "--repository", str(self.repository),
            "--approval-receipt", str(receipt_path),
            "--decomposition", str(decomposition_path),
            "--graph-attempt-id", graph_attempt_id,
            "--launcher", LAUNCHER_REFERENCE,
            "--run-root", str(run_root),
            "--on-block-argv", json.dumps([sys.executable, "-c", "pass"]),
        ]
        run_result = driver.run_graph(argv=run_argv)
        self.assertTrue(
            run_result["status_flags"]["success"],
            f"repair graph did not succeed: {run_result}",
        )
        final_candidate = run_result["candidate_commit"]
        self.assertIsNotNone(final_candidate)

        # -- deterministic join, proven from PlanGraph's own per-node audit
        # record (run_root/<attempt>/checkpoint.json state.nodes[*]), not
        # from close()'s own join_sealed flag or from content assertions
        # alone (see the module docstring: those would hold identically
        # under sequential dispatch even had the join node never run). -----
        attempt_checkpoint = json.loads(
            (run_root / graph_attempt_id / "checkpoint.json").read_text()
        )
        nodes_state = attempt_checkpoint["state"]["nodes"]
        for _route, subject_file, repair_node_id in _ROUTE_FILES:
            node_state = nodes_state[repair_node_id]
            self.assertEqual(
                node_state["status"], "succeeded",
                f"repair node {repair_node_id!r} did not seal: {node_state}",
            )
            node_candidate = node_state["candidate_commit"]
            self.assertTrue(node_candidate)
            node_content = self._git("show", f"{node_candidate}:{subject_file}")
            self.assertIn(
                _TARGET_COLOR_STYLE, node_content,
                f"repair node {repair_node_id!r}'s own candidate does not carry its fix",
            )
        join_state = nodes_state["join-and-regression"]
        self.assertEqual(join_state["status"], "succeeded", f"join node did not seal: {join_state}")
        self.assertEqual(
            join_state["candidate_commit"], final_candidate,
            "the join node's own sealed candidate must be the round's final candidate",
        )

        # PlanGraph's own composition -- not the launcher fixture's: the
        # round's one final candidate incorporates both repair nodes' edits,
        # via whatever base-commit sequencing PlanGraph decided (see the
        # module docstring and the launcher fixture's own docstring for why
        # this is a chained composition rather than a two-parent merge
        # commit, given scripts/run_plan_graph.py's actual, unmodified
        # capabilities). Read directly from the repository's git history,
        # not from anything the launcher or this test computed independently.
        joined_index_html = self._git("show", f"{final_candidate}:app/index.html")
        joined_about_html = self._git("show", f"{final_candidate}:app/about.html")
        self.assertIn(_TARGET_COLOR_STYLE, joined_index_html)
        self.assertIn(_TARGET_COLOR_STYLE, joined_about_html)
        # The final candidate is reachable from the round's base commit --
        # a real descendant, not a fabricated or disconnected commit.
        ancestor_check = subprocess.run(
            ["git", "-C", str(self.repository), "merge-base", "--is-ancestor", base_commit, final_candidate],
            capture_output=True,
        )
        self.assertEqual(
            ancestor_check.returncode, 0,
            "the joined final candidate does not descend from the round's base commit",
        )

        # -- close (real ``close`` subcommand): base adoption. Deliberately
        # invoked with no --next-capture-argv (the campaign CLI's own
        # nargs="+" cannot carry the real capture script's flags either --
        # see the module docstring); the post-repair measure below is
        # instead a separately invoked, real staleness-checked driver.measure()
        # call, per the ordering BaseAdoptionAutomaticMeasureWithRepositoryTests
        # establishes: checkout to the adopted candidate strictly *after*
        # this close call (so its own staleness check still compares
        # against the pre-adoption head) and *before* that separate measure
        # call (so its staleness check compares against the now-current
        # head). ---------------------------------------------------------
        run_result_path = self.root / "run-result.json"
        run_result_path.write_text(json.dumps(run_result), encoding="utf-8")
        close_result = self._campaign_cli("close", "--run-result-file", str(run_result_path))
        self.assertTrue(close_result["base_adopted"])
        self.assertEqual(close_result["new_base_commit"], final_candidate)
        self.assertTrue(close_result["join_sealed"])
        self.assertIsNone(close_result["measure_result"])

        self._git("checkout", "--detach", final_candidate)

        capture_out_2 = self.root / "capture-round-2"
        measured_2 = driver.measure(
            capture_argv=[
                sys.executable, str(CAPTURE_SCRIPT),
                "--app-dir", str(self.repository / "app"),
                "--matrix", str(matrix_path),
                "--out", str(capture_out_2),
                "--driver", "stub",
            ],
            out_dir=capture_out_2,
            runner=functools.partial(subprocess.run, env=_subprocess_env()),
        )

        # -- post-repair keyed inspection + ingest (real ``ingest``
        # subcommand) -----------------------------------------------------
        receipt_2 = measured_2["audit_result"]
        audit_result_2 = self._inspect_post_repair(receipt_2, driver)
        audit_result_2_path = self.root / "audit-result-2.json"
        audit_result_2_path.write_text(json.dumps(audit_result_2), encoding="utf-8")
        ingest_2 = self._campaign_cli("ingest", "--audit-result-file", str(audit_result_2_path))
        self.assertEqual(
            sorted(list(key) for key in ingest_2["summary"]["fixed"]),
            [["app/about.html", "page-title-light-color"], ["app/index.html", "page-title-light-color"]],
        )

        # -- per-key verdicts closing the seeded finding: termination,
        # evaluated via a second real ``close`` call's own
        # --termination-file (the round that closed already adopted its
        # base above; this second call re-observes the same, now-idempotent
        # adoption and then evaluates bounds-termination's success predicate
        # against the ledger this round's post-repair ingest just updated --
        # which could not happen in the same call as the first close, since
        # that ingest has to happen in between). ---------------------------
        termination_kwargs = {
            "required_cells": [_cell_id("index.html"), _cell_id("about.html")],
            "inspector_recall": 1.0,
            "amendment_ratio_acknowledged": True,
        }
        termination_path = self.root / "termination.json"
        termination_path.write_text(json.dumps(termination_kwargs), encoding="utf-8")
        close_result_2 = self._campaign_cli(
            "close", "--run-result-file", str(run_result_path),
            "--termination-file", str(termination_path),
        )
        self.assertTrue(close_result_2["termination"]["success"], close_result_2["termination"])

        # =====================================================================
        # AC-CC05-2: campaign directory afterward.
        # =====================================================================

        # -- schema-valid checkpoint -------------------------------------------
        checkpoint_raw = json.loads((self.campaign_root / "checkpoint.json").read_text())
        self.assertEqual(checkpoint_raw["protocol"], CHECKPOINT_PROTOCOL)
        self.assertIsInstance(checkpoint_raw["sequence"], int)
        self.assertGreater(checkpoint_raw["sequence"], 0)
        self.assertIsInstance(checkpoint_raw["base_commit"], str)
        self.assertTrue(checkpoint_raw["base_commit"])
        self.assertIsInstance(checkpoint_raw["liveness_at"], str)
        self.assertIn("state", checkpoint_raw)
        self.assertEqual(checkpoint_raw["lifecycle"], "succeeded")
        # Loading it through the store itself is the strongest schema check
        # available: a malformed protocol or missing field raises there.
        loaded_checkpoint = driver.checkpoint.load()
        self.assertEqual(loaded_checkpoint.lifecycle, "succeeded")

        # -- replayable ledger whose final state shows the seeded key
        # observed_fixed -- re-instantiated from the raw journal file, not
        # the live in-memory driver.ledger, to prove replay determinism. ----
        replayed_ledger = ConvergenceLedger(self.campaign_root / "ledger.jsonl")
        seeded_key = ("app/index.html", "page-title-light-color")
        self.assertEqual(replayed_ledger.key_status(seeded_key), "fixed")
        self.assertEqual(
            replayed_ledger.key_status(("app/about.html", "page-title-light-color")), "fixed",
        )
        self.assertEqual(replayed_ledger.open_set(), frozenset())

        # -- store-resident evidence for every ledger evidence reference -----
        evidence_refs: list[str] = []
        for record in replayed_ledger.records():
            if record.get("type") == "finding_opened":
                evidence_refs.extend(record.get("evidence_refs") or [])
        self.assertEqual(len(evidence_refs), 2, "expected exactly the two seeded findings' evidence refs")
        for ref in evidence_refs:
            self.assertTrue(ref.startswith("artifact:sha256:"), ref)
            digest = ref.rsplit(":", 1)[-1]
            self.assertTrue(
                driver.artifacts.contains(digest),
                f"evidence ref {ref!r} referenced by the ledger has no store-resident artifact",
            )

        # =====================================================================
        # Human-inputs claim: exactly operator-approval.json and the scripted
        # rule dispositions were authored on the human's behalf at flow time,
        # inside this test's temporary product repository -- checked non-
        # tautologically by enumerating the repository worktree and its
        # tracked git history, not merely by re-listing this test's own
        # writes back at themselves.
        # =====================================================================
        status = subprocess.run(
            ["git", "-C", str(self.repository), "status", "--porcelain", "--ignored"],
            text=True, capture_output=True, check=True,
        ).stdout
        status_lines = [line for line in status.splitlines() if line.strip()]
        non_ignored = [line for line in status_lines if not line.startswith("!!")]
        ignored = [line for line in status_lines if line.startswith("!!")]
        self.assertEqual(
            non_ignored, [],
            "the repository worktree must carry no untracked or modified path "
            "outside the gitignored human-inputs directory -- any such path "
            "would be an unaccounted-for human input",
        )
        self.assertEqual(
            ignored, ["!! human-inputs/"],
            "the only ignored -- and therefore only permitted untracked -- "
            "path anywhere in the repository worktree must be the "
            "human-inputs directory itself",
        )
        tracked_history = self._git("rev-list", "main").splitlines()
        self.assertEqual(
            tracked_history, [base_commit],
            "the tracked branch's history must contain only the fixture "
            "scaffold commit -- no flow step (including admission's own "
            "findings-owners-paths table commit, neutralized by the "
            "pre-seed above) may land a second one",
        )
        human_input_names = sorted(path.name for path in self.human_inputs_dir.iterdir())
        self.assertEqual(
            human_input_names, ["dispositions-round-1.json", "operator-approval.json"],
            "exactly two files may be authored on the human's behalf during "
            "the campaign flow; fixture-repo scaffolding (the app copy, "
            "matrix.json, docs/plan.md, decomposition.json, the repository "
            "identity file, and the pre-seeded findings-owners-paths.json) "
            "is committed once before the flow starts and is not a human "
            "input",
        )


if __name__ == "__main__":
    unittest.main()
