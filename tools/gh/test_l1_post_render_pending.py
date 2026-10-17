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



# Reforge (sticky-wicket 6237951432): one table, both readers, equal answers.
AGREEMENT = [
    # (name, message, repo trailer.separators or None, expected run id or None)
    ("plain trailer", "render: pending\n\nRender-Pending: 11\n", None, "11"),
    ("below a --- line", "render: pending\n\n---\nnotes\n\nRender-Pending: 42\n", None, "42"),
    ("body text, not a trailer",
     "subject\n\nRender-Pending: 1\n\nA closing paragraph that is prose.\n", None, None),
    ("beside Signed-off-by", "subject\n\nSigned-off-by: A <a@b>\nRender-Pending: 9\n", None, "9"),
    ("repo config '=:' separators", "subject\n\nRender-Pending: 7\n", "=:", "7"),
    ("no trailer", "ordinary commit\n", None, None),
]


class BothReadersAgree(StaticChecksRenderPending):
    def test_every_message_gets_the_same_answer_from_both_readers(self) -> None:
        import gate_ci
        from test_gate_ci import PASS_BODY, check, fake_gh
        for name, message, separators, expected in AGREEMENT:
            with self.subTest(name=name):
                if separators:
                    git(self.repo, "config", "trailer.separators", separators)
                else:
                    subprocess.run(["git", "config", "--unset", "trailer.separators"],
                                   cwd=self.repo, capture_output=True)
                git(self.repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim",
                    "-m", message)
                sha = git(self.repo, "rev-parse", "HEAD")
                l1 = self._static_checks(sha)
                l1_refused = "awaiting visual-baseline render run" in l1
                ok, gate_msg = gate_ci.check_gate_result(
                    "o/r", PASS_BODY,
                    run=fake_gh(checks=[check("verify")], message=message))
                self.assertEqual(l1_refused, not ok, f"{name}: l1={l1!r} gate={gate_msg!r}")
                if expected:
                    self.assertIn(expected, l1)
                    self.assertIn(expected, gate_msg)
                else:
                    self.assertTrue(ok, gate_msg)

if __name__ == "__main__":
    unittest.main()
