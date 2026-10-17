#!/usr/bin/env python3
"""harmonic-forge#1066: `static_checks` refuses a head still marked Render-Pending.

Driven against a real scratch git repository, so the trailer read is git's own,
and only the head commit is read: a marker with a commit above it passes.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class StaticChecksRenderPending(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _static_checks(self, sha: str) -> str:
        """Run static_checks up to the step after the trailer read. The next
        step (`git rev-parse --show-toplevel`) is made to fail with a known
        message, so reaching it means the trailer read did not refuse."""
        real_run = L.run

        def run(*args, **kw):
            if args[:2] == ("git", "rev-parse") and "--show-toplevel" in args:
                return subprocess.CompletedProcess(args, 1, "", "")
            return real_run(*args, **kw)

        with mock.patch.object(L, "resolve_sha", return_value=sha), \
                mock.patch.object(L, "refresh_main", return_value=self.base), \
                mock.patch.object(L, "run", side_effect=run):
            with self.assertRaises(SystemExit) as raised:
                L.static_checks(sha, "b")
        return str(raised.exception)

    def test_a_marked_head_is_refused_naming_the_run(self) -> None:
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "render: pending",
            "-m", "Render-Pending: 39013060663")
        msg = self._static_checks(git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("39013060663", msg)
        self.assertIn("lane1-rebase", msg)

    def test_a_marker_below_the_head_is_not_refused(self) -> None:
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "render: pending",
            "-m", "Render-Pending: 39013060663")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "rendered baselines")
        msg = self._static_checks(git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("cannot resolve the source worktree root", msg)

    def test_an_unmarked_head_is_not_refused(self) -> None:
        msg = self._static_checks(self.base)
        self.assertIn("cannot resolve the source worktree root", msg)


if __name__ == "__main__":
    unittest.main()
