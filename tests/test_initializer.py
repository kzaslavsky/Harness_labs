"""Black-box contract tests for the portable initializer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
import importlib.machinery
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "initialize-project"


class InitializerTests(unittest.TestCase):
    maxDiff = None

    def run_initializer(self, target: Path, *extra: str, launcher: Path = LAUNCHER) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(launcher), str(target), "--name", "Example Project", "--purpose", "A test project", *extra],
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verification: preflight completed", result.stdout)

    def run_doc_checks(self, target: Path) -> None:
        for script in ("check_doc_status.py", "check_doc_links.py"):
            result = subprocess.run(["python3", str(target / "scripts" / script)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_base_initializes_modules_skills_and_learning_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "project"
            result = self.run_initializer(target, "--module", "src/core")
            self.assert_success(result)
            self.assertTrue((target / "logs" / ".gitkeep").is_file())
            self.assertTrue((target / "src/core/context.md").is_file())
            self.assertTrue((target / "src/core/API.md").is_file())
            self.assertTrue((target / ".claude/commands/module-docs.md").is_file())
            self.assertTrue((target / ".agents/skills/module-docs/SKILL.md").is_file())
            self.assertTrue((target / "docs/governance/git-policy.md").is_file())
            self.assertNotIn("Retinology", (target / "AGENTS.md").read_text())
            self.assertNotIn("__PROJECT_", "\n".join(path.read_text() for path in target.rglob("*.md")))
            self.assertEqual(json.loads((target / "learnings/learnings.json").read_text())["entries"], [])
            regenerate = subprocess.run(
                ["python3", str(target / "learnings/scripts/regenerate_md.py"), "--project-name", "Example Project"],
                text=True,
                capture_output=True,
            )
            self.assertEqual(regenerate.returncode, 0, regenerate.stderr)
            self.run_doc_checks(target)

    def test_required_compositions(self) -> None:
        cases = (("python", "python-health"), ("web", "web-health"))
        with tempfile.TemporaryDirectory() as temp:
            for overlay, directory in cases:
                target = Path(temp) / directory
                result = self.run_initializer(
                    target, "--template", overlay, "--template", "regulated-health", "--module", "app/core"
                )
                self.assert_success(result)
                self.assertTrue((target / "docs/governance/regulated-health.md").is_file())
                self.assertTrue((target / "app/core/context.md").is_file())
                if overlay == "python":
                    self.assertTrue((target / "src/example_project/__init__.py").is_file())
                    self.assertTrue((target / "src/example_project/context.md").is_file())
                    self.assertTrue((target / "src/example_project/API.md").is_file())
                    self.assertTrue((target / "pyproject.toml").is_file())
                else:
                    self.assertTrue((target / "docs/development/ui-verification.md").is_file())
                self.run_doc_checks(target)

    def test_selected_skill_surface_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "project"
            self.assert_success(self.run_initializer(target, "--skill-surface", "codex"))
            self.assertTrue((target / ".agents/skills/implement-v13-codex/scripts/feature_queue_state.py").is_file())
            self.assertTrue((target / ".agents/skills/implement-v13-codex/scripts/run_feature.py").is_file())
            self.assertTrue((target / ".agents/skills/implement-v13-codex/schemas/closure-program.schema.json").is_file())
            self.assertFalse((target / ".claude").exists())
            inventory = (target / "docs/development/skill-inventory.md").read_text()
            self.assertIn("Codex skill", inventory)
            self.assertNotIn("Claude command", inventory)

    def test_toml_metadata_is_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "project"
            result = subprocess.run(
                [
                    str(LAUNCHER), str(target), "--name", 'Example "Project"',
                    "--purpose", 'A "quoted" \\ purpose', "--template", "python",
                ],
                text=True,
                capture_output=True,
            )
            self.assert_success(result)
            project = tomllib.loads((target / "pyproject.toml").read_text())["project"]
            self.assertEqual(project["description"], 'A "quoted" \\ purpose')

    def test_unsafe_or_conflicting_requests_leave_no_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "existing"
            existing.mkdir()
            self.assertNotEqual(self.run_initializer(existing).returncode, 0)

            traversal = root / "traversal"
            self.assertNotEqual(self.run_initializer(traversal, "--module", "../outside").returncode, 0)
            self.assertFalse(traversal.exists())

            duplicate = root / "duplicate"
            self.assertNotEqual(
                self.run_initializer(duplicate, "--template", "python", "--template", "python").returncode, 0
            )
            self.assertFalse(duplicate.exists())

            bad_metadata = root / "bad-metadata"
            result = subprocess.run(
                [str(LAUNCHER), str(bad_metadata), "--name", "bad\nname", "--purpose", "purpose"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(bad_metadata.exists())

            destination = root / "destination"
            destination.mkdir()
            symlink_target = root / "symlink"
            os.symlink(destination, symlink_target)
            self.assertNotEqual(self.run_initializer(symlink_target).returncode, 0)
            self.assertFalse((destination / "AGENTS.md").exists())

    def test_undeclared_template_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "package"
            shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            collision = package / "templates/collision"
            collision.mkdir()
            (collision / "manifest.json").write_text(json.dumps({"id": "collision", "requires": ["base"], "overrides": []}))
            (collision / "README.md").write_text("collision")
            target = Path(temp) / "target"
            result = self.run_initializer(target, "--template", "collision", launcher=package / "bin/initialize-project")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("undeclared template collision", result.stderr)
            self.assertFalse(target.exists())

    def test_generated_skill_files_do_not_overwrite_template_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "package"
            shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            collision = package / "templates/collision"
            collision.mkdir()
            (collision / "manifest.json").write_text(json.dumps({"id": "collision", "requires": ["base"], "overrides": []}))
            inventory = collision / "docs/development/skill-inventory.md"
            inventory.parent.mkdir(parents=True)
            inventory.write_text("template content")
            target = Path(temp) / "target"
            result = self.run_initializer(target, "--template", "collision", launcher=package / "bin/initialize-project")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("generated skill inventory collides", result.stderr)
            self.assertFalse(target.exists())

    def test_write_refuses_target_created_after_preflight(self) -> None:
        loader = importlib.machinery.SourceFileLoader("initializer", str(LAUNCHER))
        spec = importlib.util.spec_from_loader("initializer", loader)
        self.assertIsNotNone(spec)
        initializer = importlib.util.module_from_spec(spec)
        loader.exec_module(initializer)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target"
            target.mkdir()
            sentinel = target / "keep"
            sentinel.write_text("unchanged")
            with self.assertRaises(initializer.InitializationError):
                initializer.write_atomically(target, {Path("replacement"): b"new"})
            self.assertEqual(sentinel.read_text(), "unchanged")
            self.assertFalse((target / "replacement").exists())

    def test_template_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "package"
            shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            os.symlink(package / "templates/base/README.md", package / "templates/web/unsafe-link.md")
            target = Path(temp) / "target"
            result = self.run_initializer(target, "--template", "web", launcher=package / "bin/initialize-project")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("template symlinks", result.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
