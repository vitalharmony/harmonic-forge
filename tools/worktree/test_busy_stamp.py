#!/usr/bin/env python3
"""Tests for the busy-lock integrity step: `begin`'s stamp and `end`'s three
outcomes (harmonic-forge#1560, AC2, sticky-wicket fixes A and F).

Every stamp lives in a temp `WORKTREE_BUSY_DIR`; nothing touches
~/.claude/state/worktree-busy.
"""
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import busy_lock  # noqa: E402
import busy_stamp  # noqa: E402


class Repo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = self.base / "store"
        env = mock.patch.dict(os.environ, {"WORKTREE_BUSY_DIR": str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.base / "w"
        self.root.mkdir()
        g = ["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t"]
        self.git = lambda *a: subprocess.run([*g, *a], check=True, capture_output=True, text=True)
        (self.root / "a.txt").write_text("a\n")
        (self.root / "b.txt").write_text("b\n")
        (self.root / "d").mkdir()
        (self.root / "d" / "c.txt").write_text("c\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "x")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        root = self.root
        self.addCleanup(lambda: os.chmod(root / "d", 0o755) if (root / "d").is_dir() else None)

    def begin(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(busy_lock.main(["begin", "--root", str(self.root), "--pid", str(os.getpid())]), 0)
        (assignment,) = shlex.split(out.getvalue())
        self.assertTrue(assignment.startswith("nonce="), out.getvalue())
        time.sleep(0.01)
        return assignment.split("=", 1)[1]

    def end(self, nonce: str) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return busy_lock.main(["end", "--root", str(self.root), "--head", self.head, "--nonce", nonce])


class Integrity(Repo):
    """AC2: `end` fails when the worktree changed between `begin` and `end`."""

    def test_unchanged_passes(self):
        self.assertEqual(self.end(self.begin()), 0)

    def test_dirty_at_start_and_unchanged_passes(self):
        (self.root / "a.txt").write_text("dirty\n")
        self.assertEqual(self.end(self.begin()), 0)

    def test_head_moved_fails(self):
        nonce = self.begin()
        self.git("commit", "-q", "--allow-empty", "-m", "y")
        self.assertEqual(self.end(nonce), 1)

    def test_partial_commit_of_a_dirty_tree_fails(self):
        (self.root / "a.txt").write_text("a2\n")
        (self.root / "b.txt").write_text("b2\n")
        nonce = self.begin()
        self.git("commit", "-q", "-m", "part", "a.txt")
        self.git("reset", "-q", "--soft", self.head)  # HEAD back, porcelain differs
        self.assertEqual(self.end(nonce), 1)

    def test_untracked_file_created_mid_run_fails(self):
        nonce = self.begin()
        (self.root / "new.txt").write_text("n\n")
        self.assertEqual(self.end(nonce), 1)

    def test_edit_then_revert_fails(self):
        nonce = self.begin()
        (self.root / "a.txt").write_text("a\n")  # same content, rewritten inode
        self.assertEqual(self.end(nonce), 1)

    def test_checkout_away_and_back_fails(self):
        """The checkouts alone fail it: `other` holds different content, so git
        rewrites a.txt twice and its ctime moves; no touch."""
        self.git("checkout", "-q", "-b", "other")
        (self.root / "a.txt").write_text("other\n")
        self.git("commit", "-q", "-am", "other")
        self.git("checkout", "-q", "-")
        nonce = self.begin()
        self.git("checkout", "-q", "other")
        self.git("checkout", "-q", "-")
        self.assertEqual((self.root / "a.txt").read_text(), "a\n")
        self.assertEqual(self.end(nonce), 1)

    def test_restore_preserving_mtime_fails(self):
        path = self.root / "a.txt"
        before = os.stat(path)
        nonce = self.begin()
        path.write_text("a\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.end(nonce), 1)

    def test_chmod_mid_run_fails(self):
        nonce = self.begin()
        os.chmod(self.root / "a.txt", 0o600)
        self.assertEqual(self.end(nonce), 1)

    def odd_trees(self):
        """Fix A: each way a tracked path can already be unreadable at the start."""
        def deleted():
            (self.root / "b.txt").unlink()  # ' D b.txt', unstaged

        def skip_worktree():
            self.git("update-index", "--skip-worktree", "b.txt")
            (self.root / "b.txt").unlink()

        def enotdir():
            shutil.rmtree(self.root / "d")
            (self.root / "d").write_text("a file where a directory was\n")

        def eloop():
            shutil.rmtree(self.root / "d")
            os.symlink("d", self.root / "d")

        def eacces():
            os.chmod(self.root / "d", 0)
        cases = {"ENOENT": deleted, "skip-worktree": skip_worktree, "ENOTDIR": enotdir, "ELOOP": eloop}
        if os.geteuid() != 0:
            cases["EACCES"] = eacces
        return cases

    def test_a_tree_odd_at_the_start_and_unchanged_passes(self):
        for name, make in self.odd_trees().items():
            with self.subTest(name):
                self.setUp()
                make()
                observed = busy_stamp.observe(self.root)
                self.assertTrue(any(isinstance(v, str) for v in observed.values()), observed)
                self.assertEqual(self.end(self.begin()), 0, name)

    def test_a_tree_odd_at_the_start_then_repaired_fails(self):
        for name, make in self.odd_trees().items():
            with self.subTest(name):
                self.setUp()
                make()
                nonce = self.begin()
                os.chmod(self.root / "d", 0o755) if (self.root / "d").is_dir() else None
                if name in ("ENOTDIR", "ELOOP"):
                    os.unlink(self.root / "d")
                self.git("update-index", "--no-skip-worktree", "b.txt")
                self.git("checkout", "-q", "--", ".")
                self.assertEqual(self.end(nonce), 1, name)

    def test_tracked_path_removed_mid_run_fails(self):
        self.git("update-index", "--skip-worktree", "b.txt")
        nonce = self.begin()
        (self.root / "b.txt").unlink()  # invisible to the porcelain under skip-worktree
        self.assertEqual(self.end(nonce), 1)


class Outcomes(Repo):
    """Fix F: no nonce is unverifiable (3); a nonce with anything but this run's
    readable, matching stamp is changed (1)."""

    def test_missing_stamp_fails(self):
        nonce = self.begin()
        busy_lock.stamp_path(self.root).unlink()
        self.assertEqual(self.end(nonce), 1)

    def test_unreadable_stamp_fails(self):
        for body in ("[]", "{not json", '{"nonce": null}'):
            with self.subTest(body):
                nonce = self.begin()
                busy_lock.stamp_path(self.root).write_text(body)
                self.assertEqual(self.end(nonce), 1)

    def test_a_stale_stamp_from_an_earlier_run_fails(self):
        stale = self.begin()
        self.begin()
        self.assertEqual(self.end(stale), 1)

    def test_a_stamp_overwritten_by_a_second_run_fails_the_first(self):
        first = self.begin()
        self.begin()
        self.assertEqual(self.end(first), 1)

    def test_a_stamp_naming_another_root_fails(self):
        nonce = self.begin()
        other = self.base / "elsewhere"
        shutil.copytree(self.root, other, symlinks=True)
        os.replace(busy_lock.stamp_path(self.root), busy_lock.stamp_path(other))
        self.root = other
        self.assertEqual(self.end(nonce), 1)

    def test_a_git_error_inside_end_fails(self):
        nonce = self.begin()
        shutil.rmtree(self.root / ".git")
        self.assertEqual(self.end(nonce), 1)

    def test_no_nonce_is_unverifiable_even_after_a_change(self):
        self.begin()
        (self.root / "new.txt").write_text("n\n")
        self.assertEqual(self.end(""), 3)

    def assert_begin_unverifiable(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(busy_lock.main(["begin", "--root", str(self.root), "--pid", str(os.getpid())]), 0)
        self.assertEqual(out.getvalue(), "nonce=''\n")
        self.assertIn("no receipt: integrity stamp unavailable", err.getvalue())

    def test_a_read_only_store_gives_an_empty_nonce(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.store.mkdir()
        os.chmod(self.store, 0o500)
        self.addCleanup(os.chmod, self.store, 0o700)
        self.assert_begin_unverifiable()

    def test_any_exception_in_the_stamp_gives_an_empty_nonce(self):
        for exc in (OSError("disk"), RuntimeError("boom"), subprocess.CalledProcessError(1, "git")):
            with self.subTest(exc=exc), mock.patch.object(busy_stamp, "write_stamp", side_effect=exc):
                self.assert_begin_unverifiable()


if __name__ == "__main__":
    unittest.main()
