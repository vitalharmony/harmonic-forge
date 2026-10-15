#!/usr/bin/env python3
"""Tests for the closed-issue worktree sweep (harmonic-forge#1004).

Real git worktrees in temp directories; the gh call, the identity layer and
`/proc` are seams. One test per kill check in the issue's handoff.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import worktree_gc as gc  # noqa: E402


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.main = base / "main"
        self.root = base / "worktrees"
        self.root.mkdir()
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "config", "user.email", "t@example.test")
        git(self.main, "config", "user.name", "t")
        git(self.main, "remote", "add", "origin", "https://github.com/acme/widgets.git")
        (self.main / ".gitignore").write_text("backend/.topic_runs/\nnode_modules\n.env\nfrontend/.env\n")
        (self.main / "app.py").write_text("x = 1\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-q", "-m", "init")
        self.state = "closed"
        self.patches = [
            mock.patch.object(gc, "issue_state", side_effect=lambda repo, issue: self._state()),
            mock.patch.object(gc, "_busy", return_value=[]),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _state(self):
        if isinstance(self.state, BaseException):
            raise self.state
        return self.state

    def worktree(self, name="widgets-12-impl", branch="fix/12-thing") -> Path:
        path = self.root / name
        git(self.main, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def run_sweep(self, execute=True) -> str:
        return "\n".join(gc.sweep([self.root], execute))


class RegisteredTests(Fixture):
    def test_closed_clean_worktree_is_removed_and_branch_kept(self):
        path = self.worktree()
        self.assertIn("removed (acme/widgets#12 closed)", self.run_sweep())
        self.assertFalse(path.exists())
        branches = subprocess.run(["git", "-C", str(self.main), "branch"], capture_output=True, text=True).stdout
        self.assertIn("fix/12-thing", branches)

    def test_open_issues_clean_worktree_is_kept(self):
        """Kill check 1: treat every issue as closed."""
        path = self.worktree()
        self.state = "open"
        self.assertIn("kept: acme/widgets#12 open", self.run_sweep())
        self.assertTrue(path.exists())

    def test_untracked_file_is_reported_not_removed(self):
        """Kill check 2: drop the porcelain check."""
        path = self.worktree()
        (path / "notes.md").write_text("work\n")
        self.assertIn("reported: uncommitted", self.run_sweep())
        self.assertTrue((path / "notes.md").exists())

    def test_live_process_inside_keeps_it(self):
        """Kill check 3: drop the busy check."""
        path = self.worktree()
        with mock.patch.object(gc, "_busy", return_value=[(4242, "bash")]):
            self.assertIn("kept: busy pid 4242", self.run_sweep())
        self.assertTrue(path.exists())

    def test_gh_or_identity_failure_is_skipped(self):
        """Kill check 4: a failed read must never count as closed."""
        path = self.worktree()
        for failure in (RuntimeError("gh acme/widgets#12: HTTP 404"), gc.ManifestError("no account")):
            self.state = failure
            self.assertIn("skipped: acme/widgets#12", self.run_sweep())
        self.assertTrue(path.exists())

    def test_gh_nonzero_exit_raises_through_issue_state(self):
        self.patches[0].stop()
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 404")
        with mock.patch.object(gc, "slot_env", return_value={}), \
                mock.patch.object(gc.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError):
                gc.issue_state("acme/widgets", 12)
        self.patches[0].start()

    def test_ignored_run_receipt_is_reported_not_removed(self):
        """Kill check 6: drop the ignored-files check."""
        path = self.worktree()
        receipt = path / "backend" / ".topic_runs" / "run-1.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{}")
        self.assertIn("reported: non-regenerable: backend/.topic_runs/run-1.json", self.run_sweep())
        self.assertTrue(receipt.exists())

    def test_symlink_targets_survive_removal(self):
        """Kill check 7: following links on removal would delete the main checkout's files."""
        path = self.worktree()
        (self.main / "node_modules").mkdir()
        (self.main / "node_modules" / "pkg.js").write_text("keep\n")
        (self.main / ".env").write_text("SECRET=1\n")
        os.symlink(self.main / "node_modules", path / "node_modules")
        os.symlink(self.main / ".env", path / ".env")
        self.assertIn("removed", self.run_sweep())
        self.assertTrue((self.main / "node_modules" / "pkg.js").exists())
        self.assertTrue((self.main / ".env").exists())

    def test_branch_and_directory_numbers_must_agree(self):
        """Kill check 9."""
        path = self.worktree(name="widgets-12-impl", branch="fix/34-other")
        self.assertIn("skipped: dir 12 != branch 34", self.run_sweep())
        self.assertTrue(path.exists())

    def test_manifest_error_is_skipped_and_sweep_continues(self):
        """Kill check 10: a ManifestError must not end the sweep."""
        first = self.worktree(name="widgets-12-impl", branch="fix/12-a")
        second = self.worktree(name="widgets-13-impl", branch="fix/13-b")
        calls = []

        def state(repo, issue):
            calls.append(issue)
            if issue == 12:
                raise gc.ManifestError("unregistered repo")
            return "closed"
        with mock.patch.object(gc, "issue_state", side_effect=state):
            out = self.run_sweep()
        self.assertIn("widgets-12-impl: skipped: acme/widgets#12: unregistered repo", out)
        self.assertIn("widgets-13-impl: removed", out)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())

    def test_a_crashing_candidate_is_skipped_and_the_sweep_continues(self):
        """Kill check 10 (AC2): an unexpected error in one candidate (a git
        failure inside `issue_for_worktree`, say) never ends the sweep."""
        first = self.worktree(name="widgets-12-impl", branch="fix/12-a")
        second = self.worktree(name="widgets-13-impl", branch="fix/13-b")
        real = gc.resolve

        def resolve(path):
            if path.name == "widgets-12-impl":
                raise ValueError("git exploded")
            return real(path)
        with mock.patch.object(gc, "resolve", side_effect=resolve):
            out = self.run_sweep()
        self.assertIn("widgets-12-impl: skipped: ValueError: git exploded", out)
        self.assertIn("widgets-13-impl: removed", out)
        self.assertTrue(first.exists())

    def test_detached_head_with_unbranched_commit_is_skipped(self):
        """Preclose pass 1: commits on no branch would be orphaned by removal."""
        path = self.worktree()
        git(path, "config", "user.email", "t@example.test")
        git(path, "config", "user.name", "t")
        git(path, "checkout", "-q", "--detach")
        (path / "app.py").write_text("x = 2\n")
        git(path, "commit", "-qam", "unbranched work")
        self.assertIn("skipped: detached HEAD", self.run_sweep())
        self.assertTrue(path.exists())

    def test_untracked_file_is_seen_whatever_the_user_config(self):
        """Preclose pass 2: status.showUntrackedFiles=no must not hide work."""
        path = self.worktree()
        git(self.main, "config", "status.showUntrackedFiles", "no")
        (path / "precious.txt").write_text("unique\n")
        self.assertIn("reported: uncommitted", self.run_sweep())
        self.assertTrue((path / "precious.txt").exists())

    def test_skip_worktree_file_is_reported(self):
        """Sticky-wicket: a skip-worktree edit reads clean but removal deletes it."""
        path = self.worktree()
        git(path, "update-index", "--skip-worktree", "app.py")
        (path / "app.py").write_text("local edit\n")
        self.assertIn("reported: assume-unchanged or skip-worktree: app.py", self.run_sweep())
        self.assertTrue(path.exists())

    def test_unreadable_directory_inside_a_worktree_is_skipped(self):
        """Post-verdict (cross-family): git exits 0 with a warning, so the read is incomplete."""
        path = self.worktree()
        locked = path / "scratch"
        locked.mkdir()
        (locked / "work.txt").write_text("x")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.assertIn("skipped: git status", self.run_sweep())
        self.assertTrue(path.exists())

    def test_no_flag_removes_nothing(self):
        """Kill check 11: report-only is the default."""
        path = self.worktree()
        self.assertIn("would remove", self.run_sweep(execute=False))
        self.assertTrue(path.exists())

    def test_single_digit_issue_falls_back_to_directory(self):
        path = self.worktree(name="widgets-5-impl", branch="docs/5-baseline")
        self.assertIn("removed (acme/widgets#5 closed)", self.run_sweep())
        self.assertFalse(path.exists())


class LeftoverTests(Fixture):
    def leftover(self, name="widgets-77-impl") -> Path:
        path = self.root / name
        (path / "frontend" / ".vite" / "deps").mkdir(parents=True)
        (path / "frontend" / ".vite" / "deps" / "chunk.js").write_text("x")
        old = time.time() - 2 * 3600
        for dirpath, dirnames, filenames in os.walk(path):
            for name_ in dirnames + filenames + [""]:
                os.utime(Path(dirpath) / name_, (old, old), follow_symlinks=False)
        return path

    def test_regenerable_leftover_is_deleted(self):
        path = self.leftover()
        self.assertIn("deleted (leftover, all regenerable)", self.run_sweep())
        self.assertFalse(path.exists())

    def test_leftover_with_source_file_is_reported(self):
        """Kill check 5: treat every leftover as regenerable."""
        path = self.leftover()
        (path / "app.py").write_text("real work\n")
        old = time.time() - 2 * 3600
        os.utime(path / "app.py", (old, old))
        os.utime(path, (old, old))
        self.assertIn("reported: non-regenerable: app.py", self.run_sweep())
        self.assertTrue((path / "app.py").exists())

    def test_unreadable_subdirectory_is_skipped(self):
        """Preclose pass 2: a walk error is undecidable, never 'nothing to keep'."""
        path = self.leftover()
        locked = path / "secret"
        locked.mkdir()
        (locked / "notes.txt").write_text("x")
        old = time.time() - 2 * 3600
        for p in (locked / "notes.txt", locked, path):
            os.utime(p, (old, old))
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.assertIn("skipped: unreadable", self.run_sweep())
        self.assertTrue(path.exists())

    def test_young_leftover_is_kept(self):
        """Kill check 8: drop the age guard."""
        path = self.leftover()
        (path / "frontend" / ".vite" / "deps" / "new.js").write_text("x")
        self.assertIn("kept: younger than 1h", self.run_sweep())
        self.assertTrue(path.exists())

    def test_leftover_symlink_targets_survive(self):
        """Kill check 7 for leftovers: rmtree unlinks, never descends."""
        path = self.leftover()
        target = Path(self._tmp.name) / "credentials.json"
        target.write_text("{}")
        os.symlink(target, path / "credentials.json")
        old = time.time() - 2 * 3600
        os.utime(path / "credentials.json", (old, old), follow_symlinks=False)
        os.utime(path, (old, old))
        self.assertIn("deleted", self.run_sweep())
        self.assertTrue(target.exists())


class RegenerableTests(unittest.TestCase):
    def test_the_same_file_through_an_alias_is_not_a_copy(self):
        """Preclose pass 1 (cross-family): main aliasing the worktree file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "w"
            root.mkdir()
            (root / "only.txt").write_text("the only copy")
            alias = Path(tmp) / "m"
            os.symlink(root, alias)
            self.assertFalse(gc.regenerable(root, "only.txt", alias))

    def test_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, main = Path(tmp) / "w", Path(tmp) / "m"
            (root / "frontend").mkdir(parents=True)
            (main / "frontend").mkdir(parents=True)
            (root / "frontend" / ".env").write_text("A=1")
            (main / "frontend" / ".env").write_text("A=2")
            (root / "same.txt").write_text("same")
            (main / "same.txt").write_text("same")
            (root / "diff.txt").write_text("mine")
            (main / "diff.txt").write_text("theirs")
            (root / "cache").mkdir()
            (root / "cache" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55")
            self.assertTrue(gc.regenerable(root, "frontend/.env", main))
            self.assertTrue(gc.regenerable(root, "same.txt", main))
            self.assertFalse(gc.regenerable(root, "diff.txt", main))
            self.assertTrue(gc.regenerable(root, "cache/x.bin", main))
            self.assertTrue(gc.regenerable(root, "a/node_modules/b.js", None))
            self.assertTrue(gc.regenerable(root, ".mise.local.toml", None))
            self.assertFalse(gc.regenerable(root, "frontend/.env", None))


if __name__ == "__main__":
    unittest.main()
