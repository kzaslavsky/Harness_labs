"""Verification-run image capture and repair-round forwarding.

The temporary trees here are produced by *actually running pytest* rather than
by hand-building directory names. An earlier hand-built fixture spelled the
``tmp_path`` directories ``test_import_region_matches_de0``/``de1`` while pytest
really writes ``test_import_region_matches_des0``/``pho0``; nothing matched, the
capture silently fell back to the whole tree, and the test that claimed to prove
scoping proved only that six images fit in a budget of six. Deriving the layout
from pytest itself is what stops that from recurring.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import _worker_prompt
from harness_labs.featurerun.feature_run import _attach_failure_images
from harness_labs.core.verification_images import (
    CAPTURE_ENV_VAR,
    SCOPE_EMPTY,
    SCOPE_FAILING_TESTS,
    SCOPE_WHOLE_TREE_NO_IDENTIFIERS,
    _failing_test_dir_prefixes,
    attached_image_paths,
    capture_failure_images,
    pytest_basetemp_argv,
)

PYTEST_ARGV = ("/repo/.venv/bin/pytest", "-q", "tests/test_visual.py")


def _png(color: tuple[int, int, int]) -> bytes:
    """Return a minimal single-pixel PNG."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = bytes([0, *color])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# Written into the generated test module so its tests can emit real PNG bytes
# without importing anything from this file.
_PNG_HELPER = '''
import struct, zlib

def png(color):
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes([0, *color]))) + chunk(b"IEND", b""))
'''

# Two failing parametrized variants writing a reference/actual/diff trio each,
# and one passing test whose images must never be forwarded. Every file gets
# distinct bytes because the evidence catalog dedupes by digest.
_VISUAL_MODULE = (
    _PNG_HELPER
    + '''
import pytest

@pytest.mark.parametrize("viewport", ["desktop", "phone"])
def test_import_region_matches(tmp_path, viewport):
    tint = 0 if viewport == "desktop" else 16
    for offset, role in enumerate(("reference", "actual", "diff")):
        (tmp_path / f"{viewport}-{role}.png").write_bytes(
            png((tint + offset + 1, 2, 3))
        )
    assert False, "visual contract violated"


def test_something_else_entirely(tmp_path):
    (tmp_path / "unrelated.png").write_bytes(png((200, 9, 9)))
    (tmp_path / "notes.txt").write_text("not an image")
'''
)

# A failing test whose sanitized name is a strict prefix of a passing sibling's:
# pytest writes ``test_short0`` and ``test_short_but_longer_name0`` side by side.
_PREFIX_COLLISION_MODULE = (
    _PNG_HELPER
    + '''
def test_short(tmp_path):
    (tmp_path / "short-diff.png").write_bytes(png((1, 2, 3)))
    assert False, "the short one fails"


def test_short_but_longer_name(tmp_path):
    (tmp_path / "longer-diff.png").write_bytes(png((4, 5, 6)))
'''
)


@dataclass(frozen=True)
class FailingRun:
    """One real pytest run's temporary tree plus the command mapping for it."""

    basetemp: Path
    command: dict[str, object]

    @property
    def directories(self) -> set[str]:
        return {path.name for path in self.basetemp.iterdir() if path.is_dir()}


def _run_pytest(root: Path, module_source: str) -> FailingRun:
    """Run a generated pytest module and return its basetemp and result mapping."""

    project = root / "project"
    project.mkdir(parents=True)
    (project / "test_visual.py").write_text(module_source, encoding="utf-8")
    basetemp = root / "basetemp"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
            "test_visual.py",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0, completed.stdout
    return FailingRun(
        basetemp,
        {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )


@pytest.fixture(scope="module")
def visual_run(tmp_path_factory: pytest.TempPathFactory) -> FailingRun:
    return _run_pytest(tmp_path_factory.mktemp("visual"), _VISUAL_MODULE)


@pytest.fixture(scope="module")
def collision_run(tmp_path_factory: pytest.TempPathFactory) -> FailingRun:
    return _run_pytest(tmp_path_factory.mktemp("collision"), _PREFIX_COLLISION_MODULE)


def _catalog(tmp_path: Path) -> tuple[EvidenceCatalog, AuditJournal]:
    audit = AuditJournal(
        tmp_path / "run",
        "image-capture",
        actor=AuditActor("test", "controller"),
    )
    return EvidenceCatalog(audit=audit), audit


def test_the_fixture_reproduces_pytests_real_tmp_path_layout(
    visual_run: FailingRun,
) -> None:
    """Pin the fixture to pytest's own naming, not to a guess about it.

    This is the assertion the previous hand-built fixture could not make. If a
    future pytest changes how it names or truncates ``tmp_path`` directories,
    this fails here rather than quietly disabling the scoping tests below.
    """

    stems = _failing_test_dir_prefixes(visual_run.command)
    assert stems == {
        "test_import_region_matches_des",
        "test_import_region_matches_pho",
    }
    # Every stem the production rule derives must name a directory pytest
    # actually created, as that stem followed by pytest's ordinal digits.
    for stem in stems:
        assert any(
            name.startswith(stem) and name[len(stem) :].isdigit()
            for name in visual_run.directories
        ), (stem, sorted(visual_run.directories))
    assert "test_something_else_entirely0" in visual_run.directories


def test_basetemp_is_appended_only_to_pytest_commands() -> None:
    assert pytest_basetemp_argv(PYTEST_ARGV, Path("/bt")) == (
        *PYTEST_ARGV,
        "--basetemp",
        "/bt",
    )
    assert pytest_basetemp_argv(
        ("/repo/.venv/bin/python3", "-m", "pytest", "-q"), Path("/bt")
    ) == ("/repo/.venv/bin/python3", "-m", "pytest", "-q", "--basetemp", "/bt")
    # Non-pytest commands, an absent basetemp, and a caller that already chose
    # its own basetemp are all left exactly as they were.
    assert pytest_basetemp_argv(("npm", "test"), Path("/bt")) == ("npm", "test")
    assert pytest_basetemp_argv(("make", "-m", "pytest"), Path("/bt")) == (
        "make",
        "-m",
        "pytest",
    )
    assert pytest_basetemp_argv(PYTEST_ARGV, None) == PYTEST_ARGV
    declared = (*PYTEST_ARGV, "--basetemp=/chosen")
    assert pytest_basetemp_argv(declared, Path("/bt")) == declared


def test_capture_is_disabled_by_the_environment_kill_switch(
    tmp_path: Path,
    visual_run: FailingRun,
    monkeypatch,
) -> None:
    monkeypatch.setenv(CAPTURE_ENV_VAR, "0")
    evidence, _ = _catalog(tmp_path)
    assert pytest_basetemp_argv(PYTEST_ARGV, Path("/bt")) == PYTEST_ARGV
    assert not capture_failure_images(
        command=visual_run.command,
        basetemp=visual_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
    )


def test_a_passing_run_persists_nothing(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    evidence, audit = _catalog(tmp_path)
    assert not capture_failure_images(
        command={**visual_run.command, "exit_code": 0},
        basetemp=visual_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    assert list(audit.artifacts_dir.iterdir()) == []


def test_a_missing_basetemp_persists_nothing(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    evidence, _ = _catalog(tmp_path)
    assert not capture_failure_images(
        command=visual_run.command,
        basetemp=None,
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    assert not capture_failure_images(
        command=visual_run.command,
        basetemp=tmp_path / "absent",
        evidence=evidence,
        producer_task_id="verification-owner",
    )


def test_failure_images_are_persisted_and_scoped_to_failing_tests(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    evidence, audit = _catalog(tmp_path)
    captured = capture_failure_images(
        command=visual_run.command,
        basetemp=visual_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    assert captured
    assert captured.scope == SCOPE_FAILING_TESTS
    relative = [item["relative_path"] for item in captured.descriptors]
    assert all(value.startswith("test_import_region_matches_") for value in relative)
    assert not any(value.endswith(".txt") for value in relative)
    # Both failing variants are represented rather than one exhausting the budget.
    assert {value.split("/")[0] for value in relative} == {
        "test_import_region_matches_des0",
        "test_import_region_matches_pho0",
    }
    assert captured.total_bytes == sum(
        int(item["size_bytes"]) for item in captured.descriptors
    )
    for descriptor in captured.descriptors:
        assert descriptor["media_type"] == "image/png"
        stored = Path(str(descriptor["path"]))
        assert stored.is_file()
        assert stored.parent == audit.artifacts_dir
        assert evidence.metadata(str(descriptor["evidence_ref"])).sha256 == (
            descriptor["sha256"]
        )


def test_a_passing_tests_images_stay_out_even_when_the_budget_has_room(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    """The case the six-image budget structurally hid.

    The two failing variants contribute six images between them, so a limit of
    six is exhausted before the round-robin can ever reach the passing test's
    directory -- a broken scoping rule and a working one produce the identical
    result. Raising the limit above the failing tests' own image count is the
    only way to observe whether scoping is doing anything at all.
    """

    evidence, _ = _catalog(tmp_path)
    captured = capture_failure_images(
        command=visual_run.command,
        basetemp=visual_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
        limit=20,
    )
    relative = [item["relative_path"] for item in captured.descriptors]
    assert len(relative) == 6, relative
    assert not any("unrelated" in value for value in relative), relative
    assert not any(
        value.startswith("test_something_else_entirely") for value in relative
    ), relative
    assert captured.scope == SCOPE_FAILING_TESTS


def test_a_failing_tests_name_does_not_claim_a_longer_siblings_directory(
    tmp_path: Path,
    collision_run: FailingRun,
) -> None:
    """``test_short`` must not swallow ``test_short_but_longer_name0``.

    pytest's directory name is the sanitized test name plus an ordinal, so the
    only correct membership test is stem-plus-digits. A ``startswith`` test
    hands a passing sibling's pixels to the worker whenever one test's name is
    a prefix of another's -- common wherever tests are named by refinement.
    """

    assert {"test_short0", "test_short_but_longer_name0"} <= collision_run.directories
    evidence, _ = _catalog(tmp_path)
    captured = capture_failure_images(
        command=collision_run.command,
        basetemp=collision_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
        limit=20,
    )
    relative = [item["relative_path"] for item in captured.descriptors]
    assert relative == ["test_short0/short-diff.png"], relative
    assert captured.scope == SCOPE_FAILING_TESTS


def test_an_unscopable_run_reports_the_fallback_it_took(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    """The whole-tree escape hatch must announce itself.

    Keeping the fallback is deliberate -- discarding every image when scoping
    finds nothing would lose the only pixels the round produced -- but a silent
    fallback is precisely how the scoping bug stayed invisible, so the scope is
    reported and the caller audits it.
    """

    evidence, _ = _catalog(tmp_path)
    captured = capture_failure_images(
        # Output with no ``FAILED`` summary lines: nothing to scope against.
        command={"exit_code": 1, "stdout": "exited badly", "stderr": ""},
        basetemp=visual_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
        limit=20,
    )
    assert captured
    assert captured.scope == SCOPE_WHOLE_TREE_NO_IDENTIFIERS
    assert any(
        "unrelated" in item["relative_path"] for item in captured.descriptors
    )


def test_persisted_images_round_trip_into_a_repair_context(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    evidence, _ = _catalog(tmp_path)
    captured = capture_failure_images(
        command=visual_run.command,
        basetemp=visual_run.basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    recorded = {
        **visual_run.command,
        "image_artifacts": [dict(item) for item in captured.descriptors],
    }
    paths = attached_image_paths({"failed_verification": recorded})
    assert len(paths) == len(captured.descriptors)
    assert all(path.is_file() for path in paths)
    # Every context without a captured failure yields nothing at all.
    assert attached_image_paths({}) == ()
    assert attached_image_paths({"failed_verification": {"exit_code": 1}}) == ()
    assert attached_image_paths({"failed_verification": "not a mapping"}) == ()
    # A descriptor whose file has since been removed is dropped, not returned.
    paths[0].unlink()
    assert len(attached_image_paths({"failed_verification": recorded})) == (
        len(captured.descriptors) - 1
    )


def test_an_empty_capture_reports_no_scope(tmp_path: Path) -> None:
    evidence, _ = _catalog(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    captured = capture_failure_images(
        command={"exit_code": 1, "stdout": "FAILED tests/test_visual.py::test_x"},
        basetemp=empty,
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    assert not captured
    assert captured.scope == SCOPE_EMPTY


def _attachment_events(audit: AuditJournal) -> list[dict]:
    return [
        json.loads(line)
        for line in audit.events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "verification_failure_images_attached"
    ]


def test_an_attachment_audits_what_it_spent_and_how_it_scoped(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    """Capture is on by default, so its cost has to be legible after the fact.

    Without this event a run's only trace of image spend is the artifact
    directory itself, which says nothing about which round paid for it or
    whether the selection was scoped or a whole-tree fallback.
    """

    evidence, audit = _catalog(tmp_path)
    recorded: dict[str, object] = {"stage": "verify", "attempt": 3}
    _attach_failure_images(
        recorded,
        command=visual_run.command,
        basetemp=visual_run.basetemp,
        evidence=evidence,
        audit=audit,
        stage="verify",
        ordinal=3,
    )
    assert recorded["image_artifacts"]
    events = _attachment_events(audit)
    assert len(events) == 1, events
    payload = events[0]["payload"]
    assert events[0]["status"] == "succeeded"
    assert payload["scope"] == SCOPE_FAILING_TESTS
    assert payload["stage"] == "verify"
    assert payload["attempt"] == 3
    assert payload["image_count"] == 6
    assert payload["total_bytes"] > 0
    assert payload["budget"] == {
        "image_limit": 6,
        "total_bytes_limit": 8 * 1024 * 1024,
    }
    assert len(payload["evidence_refs"]) == 6


def test_a_whole_tree_fallback_is_audited_as_degraded(
    tmp_path: Path,
    visual_run: FailingRun,
) -> None:
    """The escape hatch has to be visible in the ledger, not just in the pixels."""

    evidence, audit = _catalog(tmp_path)
    _attach_failure_images(
        {},
        command={"exit_code": 1, "stdout": "exited badly", "stderr": ""},
        basetemp=visual_run.basetemp,
        evidence=evidence,
        audit=audit,
        stage="verify",
        ordinal=1,
    )
    events = _attachment_events(audit)
    assert len(events) == 1, events
    assert events[0]["status"] == "degraded"
    assert events[0]["payload"]["scope"] == SCOPE_WHOLE_TREE_NO_IDENTIFIERS


def test_a_run_with_no_images_audits_nothing(tmp_path: Path) -> None:
    evidence, audit = _catalog(tmp_path)
    recorded: dict[str, object] = {}
    _attach_failure_images(
        recorded,
        command={"exit_code": 1, "stdout": "FAILED tests/test_visual.py::test_x"},
        basetemp=None,
        evidence=evidence,
        audit=audit,
        stage="verify",
        ordinal=1,
    )
    assert recorded == {}
    assert _attachment_events(audit) == []


def test_worker_prompt_mentions_images_only_when_they_exist() -> None:
    task = {
        "id": "T-1",
        "objective": "repair the visual gate",
        "details_schema": "few-verification-repair",
        "acceptance_criteria": [],
        "required_capabilities": ["repo.write"],
    }
    plain = _worker_prompt(task, {"failed_verification": {}}, "role")
    assert "Image evidence" not in plain

    images = (Path("/run/artifacts/000001-verification-failure-image.png"),)
    attached = _worker_prompt(task, {}, "role", images)
    assert "attached to this prompt as image input" in attached
    assert str(images[0]) in attached

    readable = _worker_prompt(task, {}, "role", images, images_attached=False)
    assert "file-reading tool" in readable
    assert str(images[0]) in readable
    # The shared preamble is untouched in every variant.
    assert plain.startswith("You are one bounded worker in an audited controller run.")
    assert attached.startswith("You are one bounded worker in an audited controller run.")
