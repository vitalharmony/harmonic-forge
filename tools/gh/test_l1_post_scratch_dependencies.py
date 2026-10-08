#!/usr/bin/env python3
"""harmonic-forge#945: l1-post's scratch check links the source checkout's
dependency install, then asks the package manager whether it satisfies the
attested commit, and installs for real in the scratch when it does not."""
from __future__ import annotations

import contextlib
import io
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


class ScratchDependencies(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.scratch = Path(tmp.name) / "repo", Path(tmp.name) / "scratch"
        for directory in ("frontend/node_modules", "backend/.venv"):
            (self.repo / directory).mkdir(parents=True)
        (self.scratch / "frontend").mkdir(parents=True)
        (self.scratch / "backend").mkdir(parents=True)
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def provision(self, dependency_dir: str, answers: dict[str, int]) -> str:
        """`answers` maps a command's first two words to its exit code; anything
        unlisted exits 0."""
        def fake_run(*args: str, cwd: Path | None = None, env=None):
            self.calls.append((args, cwd))
            return _done(answers.get(" ".join(args[:2]), 0), "line\n" * 50)
        with mock.patch.object(L, "run", side_effect=fake_run), contextlib.redirect_stderr(io.StringIO()):
            return L._provision_scratch_dependency(self.repo, self.scratch, dependency_dir)

    def commands(self) -> list[str]:
        return [" ".join(args[:2]) for args, _ in self.calls]

    # AC1
    def test_a_satisfying_install_stays_linked_and_nothing_installs(self) -> None:
        self.assertEqual(self.provision("frontend/node_modules", {}), "linked")
        self.assertTrue((self.scratch / "frontend/node_modules").is_symlink())
        self.assertEqual(self.commands(), ["npm ls"])
        self.assertEqual(self.calls[0][1], self.scratch / "frontend")

    # AC2
    def test_an_unsatisfying_install_is_replaced_by_npm_ci_in_the_scratch(self) -> None:
        self.assertEqual(self.provision("frontend/node_modules", {"npm ls": 1}), "installed")
        self.assertFalse((self.scratch / "frontend/node_modules").is_symlink())
        self.assertEqual(self.commands(), ["npm ls", "npm ci"])
        self.assertEqual(self.calls[1][1], self.scratch / "frontend")

    # AC3
    def test_a_satisfying_venv_stays_linked(self) -> None:
        self.assertEqual(self.provision("backend/.venv", {}), "linked")
        self.assertTrue((self.scratch / "backend/.venv").is_symlink())
        args, cwd = self.calls[0]
        self.assertEqual(args[:5], (".venv/bin/python", "-m", "pip", "install", "--dry-run"))
        self.assertIn("--no-index", args)
        self.assertEqual(cwd, self.scratch / "backend")

    def test_an_unsatisfying_venv_is_rebuilt_with_the_pinned_python(self) -> None:
        self.assertEqual(self.provision("backend/.venv", {".venv/bin/python -m": 1}), "installed")
        self.assertFalse((self.scratch / "backend/.venv").is_symlink())
        self.assertEqual(self.calls[1][0][:4], ("mise", "exec", "--", "python3"))
        self.assertEqual(self.calls[2][0][:2], (".venv/bin/pip", "install"))

    # AC4
    def test_a_failed_install_fails_and_leaves_no_link(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.provision("frontend/node_modules", {"npm ls": 1, "npm ci": 1})
        self.assertIn("npm ci exited 1", str(raised.exception))
        self.assertFalse((self.scratch / "frontend/node_modules").exists())

    def test_a_missing_source_install_still_fails_as_before(self) -> None:
        (self.repo / "frontend/node_modules").rmdir()
        with self.assertRaises(SystemExit) as raised:
            self.provision("frontend/node_modules", {})
        self.assertIn("dependency directory is missing", str(raised.exception))

    def test_the_ready_for_l3_loop_uses_it(self) -> None:
        src = Path(L.__file__).read_text()
        block = src[src.index("for dependency_dir in HRSE_DEPENDENCY_DIRS:"):]
        block = block[:block.index("hooks-install")]
        self.assertIn("_provision_scratch_dependency(repo_root, scratch, dependency_dir)", block)
        self.assertNotIn("symlink_to(source", block)


if __name__ == "__main__":
    unittest.main()
