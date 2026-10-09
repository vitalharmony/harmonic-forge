#!/usr/bin/env python3
"""harmonic-forge#945 (reforged): l1-post's scratch check links the source
checkout's dependency install only when the manifests beside the real install
equal the attested commit's byte for byte, and installs for real in the
scratch when they differ."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_post as L  # noqa: E402


def _done(code: int, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, "")


MANIFESTS = {"frontend": ("package.json", "package-lock.json"),
             "backend": ("requirements.txt", "requirements-dev.txt")}


class ScratchDependencies(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # The tools worktree links the main checkout's install, as on Lane 1.
        self.main, self.repo, self.scratch = root / "main", root / "repo", root / "scratch"
        for workdir, names in MANIFESTS.items():
            for base in (self.main, self.scratch):
                (base / workdir).mkdir(parents=True)
                for name in names:
                    (base / workdir / name).write_text(f"{name} v1\n")
            (self.repo / workdir).mkdir(parents=True)
        (self.main / "frontend/node_modules").mkdir()
        (self.main / "backend/.venv").mkdir()
        (self.repo / "frontend/node_modules").symlink_to(self.main / "frontend/node_modules")
        (self.repo / "backend/.venv").symlink_to(self.main / "backend/.venv")
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.stderr = io.StringIO()

    def provision(self, dependency_dir: str, codes: dict[str, int] | None = None) -> str:
        codes = codes or {}

        def fake_install(command: tuple[str, ...], cwd: Path):
            self.calls.append((command, cwd))
            return _done(codes.get(" ".join(command[:2]), 0), "line\n" * 50)
        with mock.patch.object(L, "_install", side_effect=fake_install), \
                contextlib.redirect_stderr(self.stderr):
            return L._provision_scratch_dependency(self.repo, self.scratch, dependency_dir)

    def commands(self) -> list[str]:
        return [" ".join(command[:2]) for command, _ in self.calls]

    # AC1
    def test_identical_manifests_stay_linked_and_nothing_installs(self) -> None:
        self.assertEqual(self.provision("frontend/node_modules"), "linked")
        self.assertTrue((self.scratch / "frontend/node_modules").is_symlink())
        self.assertEqual(self.calls, [])

    def test_the_manifests_compared_are_the_real_installs_not_the_links(self) -> None:
        # The tools worktree's own frontend has no manifests at all; only the
        # main checkout's, beside the real install, count.
        self.assertEqual(self.provision("frontend/node_modules"), "linked")
        (self.main / "frontend/package-lock.json").write_text("moved on main\n")
        (self.scratch / "frontend/node_modules").unlink()
        self.assertEqual(self.provision("frontend/node_modules"), "installed")

    # AC2
    def test_a_changed_lockfile_is_installed_with_npm_ci_in_the_scratch(self) -> None:
        (self.scratch / "frontend/package-lock.json").write_text("branch lock\n")
        self.assertEqual(self.provision("frontend/node_modules"), "installed")
        self.assertFalse((self.scratch / "frontend/node_modules").is_symlink())
        self.assertEqual(self.calls, [(("npm", "ci"), self.scratch / "frontend")])
        self.assertIn("package-lock.json differ", self.stderr.getvalue())

    def test_a_manifest_present_on_one_side_only_differs(self) -> None:
        (self.scratch / "backend/requirements-dev.txt").unlink()
        self.assertEqual(self.provision("backend/.venv"), "installed")

    # AC3
    def test_identical_requirements_keep_the_venv_linked(self) -> None:
        self.assertEqual(self.provision("backend/.venv"), "linked")
        self.assertTrue((self.scratch / "backend/.venv").is_symlink())

    def test_changed_requirements_rebuild_the_venv_with_the_pinned_python(self) -> None:
        (self.scratch / "backend/requirements.txt").write_text("new pin\n")
        self.assertEqual(self.provision("backend/.venv"), "installed")
        self.assertEqual(self.calls, [
            (("mise", "exec", "--", "python3", "-m", "venv", ".venv"), self.scratch / "backend"),
            ((".venv/bin/pip", "install", "-r", "requirements.txt", "-r", "requirements-dev.txt"),
             self.scratch / "backend"),
        ])

    # AC4
    def test_a_failed_install_fails_with_its_output_and_leaves_no_link(self) -> None:
        (self.scratch / "frontend/package.json").write_text("changed\n")
        with self.assertRaises(SystemExit) as raised:
            self.provision("frontend/node_modules", {"npm ci": 1})
        message = str(raised.exception)
        self.assertIn("npm ci exited 1", message)
        self.assertIn("line", message)
        self.assertFalse((self.scratch / "frontend/node_modules").exists())

    def test_an_install_that_cannot_start_or_times_out_fails(self) -> None:
        for error in (FileNotFoundError("npm"), PermissionError("npm")):
            with mock.patch.object(L.subprocess, "run", side_effect=error), \
                    self.assertRaises(SystemExit) as raised:
                L._install(("npm", "ci"), self.scratch)
            self.assertIn("could not start", str(raised.exception))
        timeout = subprocess.TimeoutExpired(["npm", "ci"], L.DEPENDENCY_INSTALL_TIMEOUT_SECONDS)
        with mock.patch.object(L.subprocess, "run", side_effect=timeout), \
                self.assertRaises(SystemExit) as raised:
            L._install(("npm", "ci"), self.scratch)
        self.assertIn("timed out", str(raised.exception))

    def test_non_utf8_install_output_is_replaced_not_raised(self) -> None:
        script = self.scratch / "emit.py"
        script.write_text("import sys; sys.stdout.buffer.write(b'\\xff ok\\n')\n")
        result = L._install((sys.executable, str(script)), self.scratch)
        self.assertIn("ok", result.stdout)
        self.assertIn("�", result.stdout)  # replaced, not dropped (errors="ignore")

    def test_an_unreadable_manifest_fails_closed(self) -> None:
        # Mocked rather than chmod 000, which root (CI) still reads.
        for error in (PermissionError("denied"), OSError("io")):
            with mock.patch.object(Path, "read_bytes", side_effect=error), \
                    self.assertRaises(SystemExit) as raised:
                self.provision("frontend/node_modules")
            self.assertIn("cannot read", str(raised.exception))

    def test_a_manifest_that_is_a_directory_fails_closed(self) -> None:
        (self.scratch / "frontend/package.json").unlink()
        (self.scratch / "frontend/package.json").mkdir()
        with self.assertRaises(SystemExit) as raised:
            self.provision("frontend/node_modules")
        self.assertIn("cannot read", str(raised.exception))

    def test_the_link_targets_the_install_that_was_compared(self) -> None:
        self.assertEqual(self.provision("frontend/node_modules"), "linked")
        link = self.scratch / "frontend/node_modules"
        self.assertEqual(Path(os.readlink(link)), (self.main / "frontend/node_modules").resolve())

    def test_a_missing_source_install_still_fails_as_before(self) -> None:
        (self.repo / "frontend/node_modules").unlink()
        with self.assertRaises(SystemExit) as raised:
            self.provision("frontend/node_modules")
        self.assertIn("dependency directory is missing", str(raised.exception))

    def test_the_ready_for_l3_loop_uses_it(self) -> None:
        src = Path(L.__file__).read_text()
        block = src[src.index("for dependency_dir in HRSE_DEPENDENCY_DIRS:"):]
        block = block[:block.index("hooks-install")]
        self.assertIn("_provision_scratch_dependency(repo_root, scratch, dependency_dir)", block)
        self.assertNotIn("symlink_to(source", block)


if __name__ == "__main__":
    unittest.main()
