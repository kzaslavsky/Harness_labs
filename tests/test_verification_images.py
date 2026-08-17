"""Verification-run image capture and repair-round forwarding."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import _worker_prompt
from harness_labs.core.verification_images import (
    CAPTURE_ENV_VAR,
    attached_image_paths,
    capture_failure_images,
    pytest_basetemp_argv,
)

PYTEST_ARGV = ("/repo/.venv/bin/pytest", "-q", "tests/test_visual.py")

FAILING_STDOUT = (
    "FF\n=========================== short test summary info "
    "============================\n"
    "FAILED tests/test_visual.py::test_import_region_matches[desktop]\n"
    "FAILED tests/test_visual.py::test_import_region_matches[phone]\n"
    "2 failed, 1 passed\n"
)


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


def _catalog(tmp_path: Path) -> tuple[EvidenceCatalog, AuditJournal]:
    audit = AuditJournal(
        tmp_path / "run",
        "image-capture",
        actor=AuditActor("test", "controller"),
    )
    return EvidenceCatalog(audit=audit), audit


def _basetemp(tmp_path: Path) -> Path:
    """Build a basetemp tree shaped like pytest's own tmp_path layout."""

    basetemp = tmp_path / "basetemp"
    # pytest sanitizes the node name and truncates it to 30 characters.
    for ordinal, viewport in enumerate(("desktop", "phone")):
        directory = basetemp / f"test_import_region_matches_de{ordinal}"
        directory.mkdir(parents=True)
        # Distinct bytes per file: the catalog dedupes by digest, and this
        # fixture is about selection, not deduplication.
        base = ordinal * 16
        (directory / f"{viewport}-reference.png").write_bytes(_png((base + 1, 2, 3)))
        (directory / f"{viewport}-actual.png").write_bytes(_png((base + 4, 5, 6)))
        (directory / f"{viewport}-diff.png").write_bytes(_png((base + 7, 8, 9)))
    passing = basetemp / "test_something_else_entirely0"
    passing.mkdir(parents=True)
    (passing / "unrelated.png").write_bytes(_png((200, 9, 9)))
    (passing / "notes.txt").write_text("not an image")
    return basetemp


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
    monkeypatch,
) -> None:
    monkeypatch.setenv(CAPTURE_ENV_VAR, "0")
    evidence, _ = _catalog(tmp_path)
    assert pytest_basetemp_argv(PYTEST_ARGV, Path("/bt")) == PYTEST_ARGV
    assert (
        capture_failure_images(
            command={"exit_code": 1, "stdout": FAILING_STDOUT},
            basetemp=_basetemp(tmp_path),
            evidence=evidence,
            producer_task_id="verification-owner",
        )
        == ()
    )


def test_a_passing_run_persists_nothing(tmp_path: Path) -> None:
    evidence, audit = _catalog(tmp_path)
    assert (
        capture_failure_images(
            command={"exit_code": 0, "stdout": "3 passed"},
            basetemp=_basetemp(tmp_path),
            evidence=evidence,
            producer_task_id="verification-owner",
        )
        == ()
    )
    assert list(audit.artifacts_dir.iterdir()) == []


def test_a_missing_basetemp_persists_nothing(tmp_path: Path) -> None:
    evidence, _ = _catalog(tmp_path)
    assert (
        capture_failure_images(
            command={"exit_code": 1, "stdout": FAILING_STDOUT},
            basetemp=None,
            evidence=evidence,
            producer_task_id="verification-owner",
        )
        == ()
    )
    assert (
        capture_failure_images(
            command={"exit_code": 1, "stdout": FAILING_STDOUT},
            basetemp=tmp_path / "absent",
            evidence=evidence,
            producer_task_id="verification-owner",
        )
        == ()
    )


def test_failure_images_are_persisted_and_scoped_to_failing_tests(
    tmp_path: Path,
) -> None:
    evidence, audit = _catalog(tmp_path)
    descriptors = capture_failure_images(
        command={"exit_code": 1, "stdout": FAILING_STDOUT},
        basetemp=_basetemp(tmp_path),
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    assert descriptors
    relative = [descriptor["relative_path"] for descriptor in descriptors]
    # Only the failing tests' own tmp_path directories contribute.
    assert all(value.startswith("test_import_region_matches_de") for value in relative)
    assert not any("unrelated" in value for value in relative)
    assert not any(value.endswith(".txt") for value in relative)
    # Both failing tests are represented rather than one exhausting the budget.
    assert {value.split("/")[0] for value in relative} == {
        "test_import_region_matches_de0",
        "test_import_region_matches_de1",
    }
    for descriptor in descriptors:
        assert descriptor["media_type"] == "image/png"
        stored = Path(str(descriptor["path"]))
        assert stored.is_file()
        assert stored.parent == audit.artifacts_dir
        assert evidence.metadata(str(descriptor["evidence_ref"])).sha256 == (
            descriptor["sha256"]
        )


def test_persisted_images_round_trip_into_a_repair_context(tmp_path: Path) -> None:
    evidence, _ = _catalog(tmp_path)
    descriptors = capture_failure_images(
        command={"exit_code": 1, "stdout": FAILING_STDOUT},
        basetemp=_basetemp(tmp_path),
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    recorded = {
        "exit_code": 1,
        "stdout": FAILING_STDOUT,
        "image_artifacts": [dict(descriptor) for descriptor in descriptors],
    }
    paths = attached_image_paths({"failed_verification": recorded})
    assert len(paths) == len(descriptors)
    assert all(path.is_file() for path in paths)
    # Every context without a captured failure yields nothing at all.
    assert attached_image_paths({}) == ()
    assert attached_image_paths({"failed_verification": {"exit_code": 1}}) == ()
    assert attached_image_paths({"failed_verification": "not a mapping"}) == ()
    # A descriptor whose file has since been removed is dropped, not returned.
    paths[0].unlink()
    assert len(attached_image_paths({"failed_verification": recorded})) == (
        len(descriptors) - 1
    )


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
