"""Smoke tests for the CC-03 measurer (tests-measurer, contracts-verdicts).

Exercises ``scripts/ui_fidelity_capture.py`` against the static fixture in
``tests/fixtures/convergence_fixture_app`` as a subprocess (so the exit
contract is genuinely observed, not just simulated in-process), plus the
mandatory per-key verdict validator in
``harness_labs.core.ui_fidelity_inspector``.

The interpreter under test is resolved from ``UI_FIDELITY_PYTHON`` (falling
back to ``sys.executable``, matching the capture script's own default), and
every test that needs an honest "no real browser" path forces it
deterministically -- by pointing ``--python`` at a path that cannot be
probed for ``playwright`` -- rather than depending on whether this
particular environment happens to have one installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness_labs.core.ui_fidelity_inspector import (
    InspectorValidationError,
    validate_inspection_result,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "ui_fidelity_capture.py"
FIXTURE_APP = REPO_ROOT / "tests" / "fixtures" / "convergence_fixture_app"
MATRIX = FIXTURE_APP / "matrix.json"
MATRIX_MINIMAL = FIXTURE_APP / "matrix_minimal.json"
SANITIZERS = FIXTURE_APP / "sanitizers.py"

# Guaranteed not to resolve to a runnable interpreter, so probing it for
# playwright fails deterministically regardless of what happens to be
# installed in the environment actually running this suite.
UNPROBEABLE_PYTHON = "/no/such/interpreter-ui-fidelity-test"

RESOLVED_PYTHON = os.environ.get("UI_FIDELITY_PYTHON", sys.executable)


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing}" if existing else str(REPO_ROOT)
    )
    return env


def _run_capture(tmp_path: Path, *extra_args: str) -> tuple[subprocess.CompletedProcess, Path]:
    out_dir = tmp_path / "capture-out"
    argv = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--app-dir",
        str(FIXTURE_APP),
        "--matrix",
        str(MATRIX),
        "--out",
        str(out_dir),
        *extra_args,
    ]
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, env=_subprocess_env()
    )
    return completed, out_dir


def _receipt(out_dir: Path) -> dict:
    return json.loads((out_dir / "receipt.json").read_text(encoding="utf-8"))


def _artifact_bytes(receipt: dict, cell: dict, kind: str) -> bytes:
    """Read an artifact's persisted bytes from the audit journal's own
    ``artifacts_dir`` -- the one location ``catalog.add`` durably writes it
    (via ``AuditJournal.write_artifact``), named in the receipt as
    ``audit_run_dir`` plus each cell's ``artifact_paths[kind]``.
    """

    return (Path(receipt["audit_run_dir"]) / cell["artifact_paths"][kind]).read_bytes()


def _find_cell(receipt: dict, *, route: str, viewport: str, theme: str, interaction: str) -> dict:
    return next(
        cell
        for cell in receipt["cells"]
        if cell["route"] == route
        and cell["viewport"] == viewport
        and cell["theme"] == theme
        and cell["interaction"] == interaction
    )


# ---------------------------------------------------------------------------
# AC-CC03-1: matrix walk, per-cell evidence, --python / UI_FIDELITY_PYTHON
# resolution, stub-vs-real recording
# ---------------------------------------------------------------------------


def test_matrix_walk_records_per_cell_evidence(tmp_path):
    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)

    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    expected_cells = (
        len(matrix["routes"])
        * len(matrix["viewports"])
        * len(matrix["themes"])
        * len(matrix["interactions"])
    )
    assert len(receipt["cells"]) == expected_cells

    ok_cell = next(cell for cell in receipt["cells"] if cell["status"] == "ok")
    assert set(ok_cell["artifacts"]) == {
        "screenshot",
        "dom_snapshot",
        "computed_styles",
        "aria_snapshot",
        "console_log",
        "network_log",
    }
    audit_run_dir = Path(receipt["audit_run_dir"])
    for kind, ref in ok_cell["artifacts"].items():
        assert ref.startswith("artifact:sha256:")
        assert (audit_run_dir / ok_cell["artifact_paths"][kind]).is_file()


def test_screenshot_evidence_reuses_verification_images_selection_and_budget(tmp_path):
    """Screenshot persistence genuinely runs through
    ``harness_labs.core.verification_images.capture_failure_images``
    (build-order-cc-03's reuse mandate), not merely importing it unused: the
    receipt's per-cell ``screenshot_evidence`` reflects that module's own
    selection accounting, and the run-level ``evidence.add_dir`` grant is
    populated with the real directory the selected screenshots live in.
    """

    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)

    ok_cell = next(cell for cell in receipt["cells"] if cell["status"] == "ok")
    evidence = ok_cell["screenshot_evidence"]
    assert evidence is not None
    assert evidence["selected"] == 1
    assert evidence["scope"]

    summary = receipt["evidence"]
    assert summary["screenshots_selected_via_verification_images"] >= 1
    audit_run_dir = Path(receipt["audit_run_dir"])
    assert summary["add_dir"] == [str((audit_run_dir / "artifacts").resolve())]
    screenshot_path = ok_cell["artifact_paths"]["screenshot"]
    assert (audit_run_dir / screenshot_path).is_file()
    assert (audit_run_dir / screenshot_path).parent == Path(summary["add_dir"][0])


def test_per_cell_evidence_content_reflects_the_interaction(tmp_path):
    """A measurer that ignored the interaction dimension entirely would
    still pass a shape-only check (every cell has six artifact keys, each
    backed by a file). This asserts the *content* of at least one evidence
    kind actually depends on which interaction ran, not just its presence.
    """

    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)

    no_interaction = _find_cell(
        receipt, route="index.html", viewport="desktop", theme="light", interaction="none"
    )
    toggled = _find_cell(
        receipt, route="index.html", viewport="desktop", theme="light", interaction="toggle-menu"
    )

    none_console = _artifact_bytes(receipt, no_interaction, "console_log")
    toggled_console = _artifact_bytes(receipt, toggled, "console_log")
    assert none_console != toggled_console
    assert b"menu-toggle" in toggled_console

    none_network = json.loads(_artifact_bytes(receipt, no_interaction, "network_log"))
    assert none_network == [
        {
            "url": "index.html",
            "method": "GET",
            "status": 200,
            "note": "stub-driver: no live network capture, static file read only",
        }
    ]


def test_default_python_resolves_to_sys_executable(tmp_path):
    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert receipt["driver"]["python"] == sys.executable


def test_python_flag_resolves_from_ui_fidelity_python_env(tmp_path):
    """UI_FIDELITY_PYTHON must gate real behaviour, not just be echoed back.

    A forced ``--driver stub`` run proves nothing about the interpreter: the
    receipt would echo ``RESOLVED_PYTHON`` even if every driver-selection
    subprocess still hardcoded ``sys.executable``. Using ``--driver auto``
    makes driver selection depend on whether *this* interpreter can import
    playwright (probed via ``_real_browser_ready``, the same check the
    script performs internally), so the assertion actually exercises the
    knob (AC-CC03-1).
    """
    out_dir = tmp_path / "capture-out"
    argv = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--app-dir",
        str(FIXTURE_APP),
        "--matrix",
        str(MATRIX_MINIMAL),
        "--out",
        str(out_dir),
        "--driver",
        "auto",
        "--python",
        RESOLVED_PYTHON,
    ]
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, env=_subprocess_env()
    )
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert receipt["driver"]["python"] == RESOLVED_PYTHON
    if _real_browser_ready(RESOLVED_PYTHON):
        assert receipt["driver"]["kind"] == "real"
        assert receipt["driver"]["skip_reason"] is None
    else:
        assert receipt["driver"]["kind"] == "stub"
        assert receipt["driver"]["skip_reason"], (
            "skip reason must be recorded, not silently absent, when "
            "UI_FIDELITY_PYTHON resolves to an interpreter with no real "
            "browser available"
        )


def test_forced_stub_driver_is_recorded_without_skip_reason(tmp_path):
    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert receipt["driver"]["kind"] == "stub"
    assert receipt["driver"]["requested"] == "stub"


def test_auto_driver_falls_back_to_stub_with_skip_reason_when_unavailable(tmp_path):
    """No real browser available under the resolved interpreter (AC-CC03-1).

    Forces unavailability deterministically via an unprobeable ``--python``
    path, so this does not depend on whether the sandbox running the suite
    happens to have a real browser installed.
    """

    completed, out_dir = _run_capture(
        tmp_path, "--driver", "auto", "--python", UNPROBEABLE_PYTHON
    )
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert receipt["driver"]["kind"] == "stub"
    assert receipt["driver"]["requested"] == "auto"
    assert receipt["driver"]["skip_reason"], "skip reason must be recorded, not silently absent"


def _real_browser_ready(python_path: str) -> bool:
    """Probe -- under ``python_path``, not this test process's own
    interpreter -- whether a real browser can actually launch.

    This is the same interpreter the capture script is told to use below via
    ``--python``, so ``UI_FIDELITY_PYTHON`` genuinely gates whether this test
    runs and drives what it exercises, instead of being resolved once and
    then never actually used for anything (AC-CC03-1).
    """

    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as playwright:\n"
        "    browser = playwright.chromium.launch()\n"
        "    browser.close()\n"
    )
    try:
        completed = subprocess.run(
            [python_path, "-c", probe], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@pytest.mark.skipif(
    not _real_browser_ready(RESOLVED_PYTHON),
    reason=(
        "no real browser available under the UI_FIDELITY_PYTHON-resolved "
        f"interpreter {RESOLVED_PYTHON!r}"
    ),
)
def test_real_browser_driver_executes_and_is_recorded(tmp_path):
    out_dir = tmp_path / "capture-out"
    argv = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--app-dir",
        str(FIXTURE_APP),
        "--matrix",
        str(MATRIX_MINIMAL),
        "--out",
        str(out_dir),
        "--driver",
        "real",
        "--python",
        RESOLVED_PYTHON,
    ]
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, env=_subprocess_env()
    )
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert receipt["driver"]["kind"] == "real"
    assert receipt["driver"]["python"] == RESOLVED_PYTHON
    assert receipt["driver"]["skip_reason"] is None
    assert len(receipt["cells"]) == 2
    for cell in receipt["cells"]:
        assert cell["status"] in {"ok", "unstable"}


# ---------------------------------------------------------------------------
# AC-CC03-2: exit contract, per-cell coverage statuses
# ---------------------------------------------------------------------------


def test_exit_zero_whenever_capture_ran_regardless_of_cell_status(tmp_path):
    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    statuses = {cell["status"] for cell in receipt["cells"]}
    # The fixture's matrix.json deliberately includes an ok route
    # (index.html), an unreachable one (missing.html), and a flaky one
    # (flaky.html) that must land unstable -- so all three honest coverage
    # statuses are actually observed by this one run, not merely possible.
    assert statuses == {"ok", "unreachable", "unstable"}


def test_missing_route_is_recorded_unreachable(tmp_path):
    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    unreachable = [cell for cell in receipt["cells"] if cell["route"] == "missing.html"]
    assert unreachable
    assert all(cell["status"] == "unreachable" for cell in unreachable)
    assert all(cell["reason"] for cell in unreachable)


def test_exit_nonzero_on_browser_launch_failure(tmp_path):
    completed, out_dir = _run_capture(
        tmp_path, "--driver", "real", "--python", UNPROBEABLE_PYTHON
    )
    assert completed.returncode != 0
    receipt = _receipt(out_dir)
    assert receipt["error"]["kind"] == "browser_launch_failure"


def test_malformed_matrix_entry_fails_only_its_cell_not_the_run(tmp_path):
    """A viewport (or interaction) object missing its declared ``name`` must
    not escape as an uncaught ``KeyError`` outside the exit contract -- it
    fails only that one cell, recorded ``unreachable``, same as any other
    per-cell fault (AC-CC03-2).
    """

    matrix = {
        "routes": ["index.html"],
        "viewports": [{"width": 800, "height": 600}],  # no "name"
        "themes": ["light"],
        "interactions": [{"name": "none", "steps": []}],
        "selectors": ["h1"],
    }
    matrix_path = tmp_path / "malformed-matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    out_dir = tmp_path / "capture-out"
    argv = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--app-dir",
        str(FIXTURE_APP),
        "--matrix",
        str(matrix_path),
        "--out",
        str(out_dir),
        "--driver",
        "stub",
    ]
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, env=_subprocess_env()
    )
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert len(receipt["cells"]) == 1
    cell = receipt["cells"][0]
    assert cell["status"] == "unreachable"
    assert "name" in cell["reason"]


# ---------------------------------------------------------------------------
# AC-CC03-3: end-state stability re-read
# ---------------------------------------------------------------------------


def test_unstable_detection_when_two_end_state_reads_differ(tmp_path):
    completed, out_dir = _run_capture(tmp_path, "--driver", "stub")
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    flaky_cells = [cell for cell in receipt["cells"] if cell["route"] == "flaky.html"]
    assert flaky_cells
    for cell in flaky_cells:
        assert cell["status"] == "unstable"
        digests = cell["end_state_digests"]
        assert digests["read_1"] != digests["read_2"]

    stable_cells = [cell for cell in receipt["cells"] if cell["route"] == "index.html"]
    assert stable_cells
    for cell in stable_cells:
        assert cell["status"] == "ok"
        digests = cell["end_state_digests"]
        assert digests["read_1"] == digests["read_2"]


# ---------------------------------------------------------------------------
# AC-CC03-4: sanitizer hook, ordering, and exit contract
# ---------------------------------------------------------------------------


def test_exit_nonzero_on_sanitizer_failure(tmp_path):
    completed, out_dir = _run_capture(
        tmp_path,
        "--driver",
        "stub",
        "--sanitizer",
        f"{SANITIZERS}:failing_sanitizer",
    )
    assert completed.returncode != 0
    receipt = _receipt(out_dir)
    assert receipt["error"]["kind"] == "sanitizer_failure"
    # failing_sanitizer rejects the very first artifact of the very first
    # cell, so for *this* sanitizer the audit journal's artifacts directory
    # (created empty when the journal opens, before any cell runs) never
    # gains a single entry -- that is a property of this sanitizer failing
    # immediately, not of an abort in general. A sanitizer that fails
    # partway through leaves earlier cells' artifacts on disk; see
    # test_sanitizer_failure_on_later_cell_leaves_earlier_artifacts_persisted.
    assert not any((out_dir / "audit" / "artifacts").iterdir())


def test_sanitizer_failure_on_later_cell_leaves_earlier_artifacts_persisted(tmp_path):
    """The abort AC-CC03-4 requires is not a rollback: per-cell artifacts are
    persisted (and journaled) as each cell is processed, so a sanitizer that
    only starts rejecting partway through the matrix leaves every earlier
    cell's artifacts on disk after the run aborts.
    """

    completed, out_dir = _run_capture(
        tmp_path,
        "--driver",
        "stub",
        "--sanitizer",
        f"{SANITIZERS}:fails_after_first_cell",
    )
    assert completed.returncode != 0
    receipt = _receipt(out_dir)
    assert receipt["error"]["kind"] == "sanitizer_failure"
    artifacts_dir = out_dir / "audit" / "artifacts"
    assert artifacts_dir.is_dir()
    assert any(artifacts_dir.iterdir())


def test_identity_sanitizer_leaves_capture_working(tmp_path):
    completed, out_dir = _run_capture(
        tmp_path,
        "--driver",
        "stub",
        "--sanitizer",
        f"{SANITIZERS}:identity_sanitizer",
    )
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    assert receipt["cells"]


def test_sanitizer_runs_before_journaling_and_digesting(tmp_path):
    """The persisted artifact and its evidence digest reflect the
    sanitizer's output, not the raw capture -- proving the sanitizer ran
    before either the digest (AC-CC03-4: "before it is ... digested") or the
    on-disk journal entry (AC-CC03-4: "before it is journaled") were
    produced.
    """

    completed, out_dir = _run_capture(
        tmp_path,
        "--driver",
        "stub",
        "--sanitizer",
        f"{SANITIZERS}:marking_sanitizer",
    )
    assert completed.returncode == 0, completed.stderr
    receipt = _receipt(out_dir)
    cell = next(cell for cell in receipt["cells"] if cell["status"] == "ok")
    ref = cell["artifacts"]["console_log"]
    digest = ref.rsplit(":", 1)[1]
    persisted = _artifact_bytes(receipt, cell, "console_log")

    assert persisted.endswith(b"<!-- sanitized -->")
    import hashlib

    assert hashlib.sha256(persisted).hexdigest() == digest


# ---------------------------------------------------------------------------
# AC-CC03-5: inspector output validator, mandatory per-key verdicts
# ---------------------------------------------------------------------------


def test_inspector_rejects_result_missing_a_prior_key_verdict():
    result = {
        "verdicts": [
            {
                "key": ["scripts/ui_fidelity_capture.py", "layout-shift"],
                "verdict": "observed_fixed",
                "capture_cell": "index.html|desktop|light|none",
                "assertion": "h1 color matches target",
            }
        ]
    }
    prior_keys = [
        ("scripts/ui_fidelity_capture.py", "layout-shift"),
        ("scripts/ui_fidelity_capture.py", "contrast-ratio"),
    ]
    with pytest.raises(InspectorValidationError, match="contrast-ratio"):
        validate_inspection_result(result, prior_keys=prior_keys)


def test_inspector_accepts_result_covering_every_prior_key_including_unobserved():
    result = {
        "verdicts": [
            {
                "key": ["scripts/ui_fidelity_capture.py", "layout-shift"],
                "verdict": "observed_fixed",
                "capture_cell": "index.html|desktop|light|none",
                "assertion": "h1 color matches target",
            },
            {
                "key": ["scripts/ui_fidelity_capture.py", "contrast-ratio"],
                "verdict": "unobserved",
            },
        ]
    }
    prior_keys = [
        ("scripts/ui_fidelity_capture.py", "layout-shift"),
        ("scripts/ui_fidelity_capture.py", "contrast-ratio"),
    ]
    validated = validate_inspection_result(result, prior_keys=prior_keys)
    assert set(validated) == set(prior_keys)


def test_inspector_rejects_invalid_verdict_kind():
    result = {
        "verdicts": [
            {"key": ["f", "s"], "verdict": "not-a-real-verdict"},
        ]
    }
    with pytest.raises(InspectorValidationError):
        validate_inspection_result(result, prior_keys=[("f", "s")])


def test_inspector_rejects_observed_fixed_without_capture_cell_citation():
    result = {
        "verdicts": [
            {"key": ["f", "s"], "verdict": "observed_fixed", "assertion": "x"},
        ]
    }
    with pytest.raises(InspectorValidationError, match="capture_cell"):
        validate_inspection_result(result, prior_keys=[("f", "s")])


def test_inspector_accepts_empty_prior_keys_with_empty_verdicts():
    result = {"verdicts": []}
    assert validate_inspection_result(result, prior_keys=[]) == {}


def test_inspector_coerces_json_decoded_list_prior_keys():
    """``prior_keys`` arrives from a JSON-decoded task context as ``[file,
    subject]`` lists, not Python tuples -- that realistic shape must not
    raise a raw ``TypeError`` from an unhashable list.
    """

    result = {
        "verdicts": [
            {
                "key": ["scripts/ui_fidelity_capture.py", "layout-shift"],
                "verdict": "unobserved",
            },
        ]
    }
    prior_keys = [["scripts/ui_fidelity_capture.py", "layout-shift"]]
    validated = validate_inspection_result(result, prior_keys=prior_keys)
    assert set(validated) == {("scripts/ui_fidelity_capture.py", "layout-shift")}


def test_inspector_rejects_missing_verdict_for_list_shaped_prior_key():
    result = {"verdicts": []}
    prior_keys = [["scripts/ui_fidelity_capture.py", "contrast-ratio"]]
    with pytest.raises(InspectorValidationError, match="contrast-ratio"):
        validate_inspection_result(result, prior_keys=prior_keys)
