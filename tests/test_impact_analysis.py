"""Tests for the read-only impact-analysis core (PlanGraph node EM-A).

Fixture repositories are built entirely inside pytest's ``tmp_path``/
``tmp_path_factory`` trees, never committed. AC-EM-4's module-scoped fixture
guards that the analysis, which reads only through the injected ``source``
callable, never touches the fixture tree on disk: it records a manifest right
after construction and re-asserts it byte-for-byte (including the absence of
stray directories such as ``__pycache__``) in teardown.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from harness_labs.plangraph.impact_analysis import (
    ImpactAssessment,
    ModuleNeighborhood,
    assess_required_paths,
    module_neighborhood,
)

# Fixture repository layout, relative to its root:
#
#   pkg/target.py        - the analysis target
#   pkg/dependency.py     - imported by pkg/target.py            (edge: imports)
#   pkg/importer_top.py   - imports pkg.target at module scope    (edge: imported_by)
#   pkg/importer_deferred.py - imports pkg.target inside a function body
#                              (edge: imported_by, deferred import)
#   pkg/unrelated.py      - imports nothing relevant
#   pkg/broken.py          - a .py file with a syntax error
#   README.md              - a non-Python file
_FIXTURE_FILES = {
    "pkg/target.py": "from pkg import dependency\n\n\ndef use():\n    return dependency.VALUE\n",
    "pkg/dependency.py": "VALUE = 1\n",
    "pkg/importer_top.py": "from pkg import target\n\n\ndef call():\n    return target.use()\n",
    "pkg/importer_deferred.py": (
        "def call_lazily():\n"
        "    import pkg.target as target\n\n"
        "    return target.use()\n"
    ),
    "pkg/unrelated.py": "VALUE = 2\n",
    "pkg/broken.py": "def broken(:\n    pass\n",
    "README.md": "# not python\n",
}


def _write_fixture_repo(root: Path) -> None:
    for relative_path, contents in _FIXTURE_FILES.items():
        full_path = root / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(contents, encoding="utf-8")


def _manifest(root: Path) -> dict:
    manifest = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _fs_source(root: Path):
    def source(relative_path: str):
        full_path = root / relative_path
        if not full_path.is_file():
            return None
        return full_path.read_bytes()

    return source


# Paths under a directory name that does not exist anywhere on disk relative
# to the test process's cwd, so that an implementation which ignored the
# injected ``source`` callable and read the filesystem directly would fail
# to find these files rather than accidentally succeeding.
_INJECTED_ONLY_FILES = {
    "zzz_injected_only_pkg/target.py": (
        "from zzz_injected_only_pkg import dependency\n\n\n"
        "def use():\n    return dependency.VALUE\n"
    ),
    "zzz_injected_only_pkg/dependency.py": "VALUE = 1\n",
    "zzz_injected_only_pkg/importer_top.py": (
        "from zzz_injected_only_pkg import target\n\n\n"
        "def call():\n    return target.use()\n"
    ),
    "zzz_injected_only_pkg/importer_deferred.py": (
        "def call_lazily():\n"
        "    import zzz_injected_only_pkg.target as target\n\n"
        "    return target.use()\n"
    ),
}


def _memory_source(files: dict):
    def source(relative_path: str):
        contents = files.get(relative_path)
        if contents is None:
            return None
        return contents.encode("utf-8")

    return source


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("impact-analysis-fixture")
    _write_fixture_repo(root)
    manifest_before = _manifest(root)

    yield root, manifest_before

    manifest_after = _manifest(root)
    assert manifest_after == manifest_before
    assert not any(root.rglob("__pycache__"))


@pytest.fixture
def repo_paths():
    return [path for path in _FIXTURE_FILES if path.endswith(".py")]


def test_module_neighborhood_finds_importers_including_deferred_and_imports(
    fixture_repo, repo_paths
):
    root, _manifest_before = fixture_repo
    source = _fs_source(root)

    neighborhood = module_neighborhood("pkg/target.py", repo_paths, source)

    assert isinstance(neighborhood, ModuleNeighborhood)
    assert neighborhood.imported_by == frozenset(
        {"pkg/importer_top.py", "pkg/importer_deferred.py"}
    )
    assert neighborhood.imports == frozenset({"pkg/dependency.py"})
    assert "pkg/unrelated.py" not in neighborhood.imported_by
    assert "pkg/unrelated.py" not in neighborhood.imports


def test_module_neighborhood_reads_exclusively_through_injected_source():
    # No file under "zzz_injected_only_pkg/" exists on disk anywhere; the
    # bytes come only from the in-memory ``source`` callable. An
    # implementation that read the filesystem directly (ignoring ``source``)
    # would find nothing here and either raise or report an empty
    # neighborhood, so this discriminates the injected-source contract.
    source = _memory_source(_INJECTED_ONLY_FILES)
    repo_paths = list(_INJECTED_ONLY_FILES)

    neighborhood = module_neighborhood(
        "zzz_injected_only_pkg/target.py", repo_paths, source
    )

    assert neighborhood.imported_by == frozenset(
        {
            "zzz_injected_only_pkg/importer_top.py",
            "zzz_injected_only_pkg/importer_deferred.py",
        }
    )
    assert neighborhood.imports == frozenset(
        {"zzz_injected_only_pkg/dependency.py"}
    )


def test_assess_required_paths_reports_missing_importer_and_confirms_the_rest(
    fixture_repo, repo_paths
):
    root, _manifest_before = fixture_repo
    source = _fs_source(root)

    # pkg/importer_deferred.py is deliberately left off required_paths.
    required_paths = [
        "pkg/target.py",
        "pkg/importer_top.py",
        "pkg/dependency.py",
    ]

    assessment = assess_required_paths(
        "pkg/target.py", required_paths, repo_paths, source
    )

    assert isinstance(assessment, ImpactAssessment)
    assert assessment.supported is True
    assert ("pkg/importer_deferred.py", "imported_by") in assessment.missing
    assert all(kind == "imported_by" for path, kind in assessment.missing)
    assert {path for path, _kind in assessment.missing} == {
        "pkg/importer_deferred.py"
    }
    assert assessment.confirmed == frozenset(
        {"pkg/importer_top.py", "pkg/dependency.py"}
    )

    # No "unrelated declared paths" concept exists on the dataclass at all.
    assert not hasattr(assessment, "unrelated")
    assert set(ImpactAssessment.__dataclass_fields__) == {
        "supported",
        "reason",
        "confirmed",
        "missing",
    }


def test_non_python_target_is_unsupported_without_raising(fixture_repo, repo_paths):
    root, _manifest_before = fixture_repo
    source = _fs_source(root)

    assessment = assess_required_paths("README.md", [], repo_paths, source)

    assert assessment.supported is False
    assert assessment.reason
    assert assessment.confirmed == frozenset()
    assert assessment.missing == ()


def test_unparseable_python_target_is_unsupported_without_raising(
    fixture_repo, repo_paths
):
    root, _manifest_before = fixture_repo
    source = _fs_source(root)

    assessment = assess_required_paths("pkg/broken.py", [], repo_paths, source)

    assert assessment.supported is False
    assert assessment.reason
    assert assessment.confirmed == frozenset()
    assert assessment.missing == ()
