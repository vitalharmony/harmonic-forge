#!/usr/bin/env python3
"""Tests for the per-worktree busy lock (harmonic-forge#1560).

Every lock here is a real `flock` held by a child bash, in a store under a
temp `WORKTREE_BUSY_DIR`; nothing touches ~/.claude/state/worktree-busy.
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import busy_lock  # noqa: E402

MARK = "# worktree-busy lock (harmonic-forge#1560)"
SCRIPT = HERE / "busy_lock.py"


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = self.base / "store"
        env = mock.patch.dict(os.environ, {"WORKTREE_BUSY_DIR": str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.base / "harmonic-forge-894-impl"
        self.root.mkdir()
        self._pids: list[int] = []
        self.addCleanup(self._reap)

    @staticmethod
    def _stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.wait()

    def _reap(self):
        for pid in self._pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def hold(self, root: Path, extra: str = "") -> subprocess.Popen:
        """A child bash holding root's lock, with the sidecar naming that shell."""
        lock = busy_lock.lock_path(root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        script = (f"exec 9>{shlex.quote(str(lock))}; flock 9; "
                  f"python3 {shlex.quote(str(SCRIPT))} sidecar --root {shlex.quote(str(root))} "
                  f"--head deadbeef; {extra} sleep 30; true")
        child = subprocess.Popen(["bash", "-c", script], env=dict(os.environ))
        self.addCleanup(self._stop, child)
        self.assertTrue(wait_for(lambda: busy_lock.sidecar_path(root).exists()), "no sidecar")
        return child


class BusyLock(Store):
    def test_held_lock_with_live_shell_is_busy(self):
        child = self.hold(self.root)
        self.assertTrue(busy_lock.is_busy(self.root))
        self.assertEqual(busy_lock.busy_roots(), [os.path.realpath(self.root)])
        record = busy_lock.read_sidecar(self.root)
        self.assertEqual(record["shell_pid"], child.pid)
        self.assertEqual(record["head"], "deadbeef")
        self.assertEqual(record["root"], os.path.realpath(self.root))

    def test_free_once_the_holder_is_killed(self):
        child = self.hold(self.root)
        child.kill()
        child.wait()
        self.assertFalse(busy_lock.is_busy(self.root))
        self.assertEqual(busy_lock.busy_roots(), [])

    def test_free_once_the_holder_exits_normally(self):
        child = self.hold(self.root)
        child.terminate()
        child.wait()
        self.assertFalse(busy_lock.is_busy(self.root))

    def test_leaked_fd_after_the_task_shell_dies_is_not_busy(self):
        """AC3 liveness: a backgrounded grandchild inherited fd 9 and outlives the shell."""
        pidfile = self.base / "grandchild.pid"
        child = self.hold(self.root, extra=f"sleep 30 & echo $! > {shlex.quote(str(pidfile))};")
        self.assertTrue(wait_for(pidfile.exists))
        self.assertTrue(wait_for(lambda: pidfile.read_text().strip() != ""))
        grandchild = int(pidfile.read_text())
        self._pids.append(grandchild)
        child.kill()
        child.wait()
        self.assertTrue(busy_lock._lock_held(busy_lock.lock_path(self.root)),
                        "the grandchild must still hold the kernel lock for this test to mean anything")
        self.assertFalse(busy_lock.is_busy(self.root))
        self.assertEqual(busy_lock.busy_roots(), [])

    def test_no_lock_no_sidecar_is_not_busy(self):
        self.assertFalse(busy_lock.is_busy(self.root))
        self.assertEqual(busy_lock.busy_roots(), [])

    def test_stale_sidecar_without_a_held_lock_is_not_busy(self):
        busy_lock.record_sidecar(self.root, "abc", os.getpid())
        busy_lock.lock_path(self.root).touch()
        self.assertFalse(busy_lock.is_busy(self.root))

    def test_corrupt_sidecar_under_a_held_lock_is_not_busy(self):
        self.hold(self.root)
        busy_lock.sidecar_path(self.root).write_text("{not json")
        self.assertFalse(busy_lock.is_busy(self.root))
        self.assertEqual(busy_lock.busy_roots(), [])

    def test_unreadable_sidecar_under_a_held_lock_is_not_busy(self):
        self.hold(self.root)
        busy_lock.sidecar_path(self.root).unlink()
        busy_lock.sidecar_path(self.root).mkdir()
        self.assertFalse(busy_lock.is_busy(self.root))
        self.assertEqual(busy_lock.busy_roots(), [])

    def test_reused_pid_with_another_start_time_is_not_busy(self):
        self.hold(self.root)
        path = busy_lock.sidecar_path(self.root)
        record = json.loads(path.read_text())
        record["shell_start"] = str(int(record["shell_start"]) + 1)
        path.write_text(json.dumps(record))
        self.assertFalse(busy_lock.is_busy(self.root))

    def test_other_worktree_is_not_busy(self):
        other = self.base / "harmonic-forge-894-impl2"
        other.mkdir()
        self.hold(self.root)
        self.assertFalse(busy_lock.is_busy(other))
        self.assertEqual(busy_lock.busy_roots(), [os.path.realpath(self.root)])

    def test_cli_path_prints_the_lock_path(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "path", "--root", str(self.root)],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, str(busy_lock.lock_path(self.root)))
        self.assertTrue(out.startswith(str(self.store)))

    def test_process_start_reads_field_22(self):
        stat = Path("/proc/self/stat").read_text()
        self.assertEqual(busy_lock.process_start(os.getpid()),
                         Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19])
        self.assertTrue(stat)
        self.assertIsNone(busy_lock.process_start(0))


class MisePrelude(Store):
    """AC5: the marked prelude in `check` and `local-check` holds the lock for the task."""

    def prelude(self, task: str) -> str:
        tasks = tomllib.loads((CHECKOUT / "mise.toml").read_text(encoding="utf-8"))["tasks"]
        marked = [line for line in tasks[task]["run"].splitlines() if line.rstrip().endswith(MARK)]
        self.assertEqual(len(marked), 1, f"[tasks.{task}] must carry exactly one marked prelude line")
        lines = tasks[task]["run"].splitlines()
        self.assertEqual([l.strip() for l in lines if l.strip()][:2], ["set -e", marked[0].strip()],
                         "the prelude is the first line after `set -e`")
        return marked[0]

    def repo(self) -> Path:
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        (repo / "tools").symlink_to(CHECKOUT / "tools")
        return repo

    def run_prelude(self, task: str, repo: Path, tail: str) -> subprocess.Popen:
        child = subprocess.Popen(["sh", "-o", "errexit", "-c", f"set -e\n{self.prelude(task)}\n{tail}"],
                                 cwd=repo, env=dict(os.environ), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(child.stderr.close)
        self.addCleanup(self._stop, child)
        return child

    def assert_holds(self, task: str):
        repo = self.repo()
        child = self.run_prelude(task, repo, "sleep 5")
        self.assertTrue(wait_for(lambda: busy_lock.is_busy(repo)), f"{task}: never busy")
        self.assertEqual(busy_lock.read_sidecar(repo)["shell_pid"], child.pid)
        child.kill()
        child.wait()
        self.assertFalse(busy_lock.is_busy(repo))

    def test_check_holds_the_lock(self):
        self.assert_holds("check")

    def test_local_check_holds_the_lock(self):
        self.assert_holds("local-check")

    def test_prelude_failure_never_fails_the_task(self):
        """A store that cannot be created reports and the task carries on."""
        repo = self.repo()
        os.environ["WORKTREE_BUSY_DIR"] = "/proc/no-such-dir/x"
        for task in ("check", "local-check"):
            child = self.run_prelude(task, repo, "echo after")
            _out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("worktree-busy: lock not taken", err)

    def test_prelude_without_the_tool_never_fails_the_task(self):
        repo = self.repo()
        (repo / "tools").unlink()
        child = self.run_prelude("check", repo, "true")
        _out, err = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, err)

    def test_a_second_check_in_the_same_worktree_refuses(self):
        """Amendment AC6 (inverted from the first design): a live holder makes
        a second check exit non-zero, naming the holder's shell pid."""
        repo = self.repo()
        outer = self.run_prelude("check", repo, "sleep 20")
        self.assertTrue(wait_for(lambda: busy_lock.is_busy(repo)))
        inner = self.run_prelude("check", repo, "echo inner-ran")
        _out, err = inner.communicate(timeout=30)
        self.assertNotEqual(inner.returncode, 0, err)
        self.assertIn("already running", err)
        self.assertIn(f"kill -- -{outer.pid}", err)
        self.assertTrue(busy_lock.is_busy(repo), "the outer check still holds the lock")

    def test_a_leaked_fd_refuses_naming_its_holder(self):
        """Amendment AC6: the task shell is gone but a child still holds fd 9."""
        repo = self.repo()
        lock = busy_lock.lock_path(repo)
        lock.parent.mkdir(parents=True, exist_ok=True)
        leaked = subprocess.Popen(["bash", "-c", f"exec 9>{lock}; flock 9; sleep 20"])
        self.addCleanup(self._stop, leaked)
        self.assertTrue(wait_for(lambda: busy_lock._lock_held(lock)))
        inner = self.run_prelude("check", repo, "echo inner-ran")
        _out, err = inner.communicate(timeout=30)
        self.assertNotEqual(inner.returncode, 0, err)
        self.assertIn("leaked fd", err)
        self.assertIn(str(leaked.pid), err)


class Integrity(Store):
    """Amendment AC2: `verify` fails when the worktree changed during the check."""

    def setUp(self):
        super().setUp()
        self.root = self.base / "w"
        self.root.mkdir()
        g = ["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t"]
        self.git = lambda *a: subprocess.run([*g, *a], check=True, capture_output=True, text=True)
        (self.root / "a.txt").write_text("a\n")
        (self.root / "b.txt").write_text("b\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.git("add", "a.txt", "b.txt")
        self.git("commit", "-q", "-m", "x")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        time.sleep(0.01)

    def verify(self) -> int:
        return busy_lock.main(["verify", "--root", str(self.root), "--head", self.head])

    def stamp(self):
        self.assertEqual(busy_lock.main(["stamp", "--root", str(self.root)]), 0)
        time.sleep(0.01)

    def test_unchanged_passes(self):
        self.stamp()
        self.assertEqual(self.verify(), 0)

    def test_dirty_at_start_and_unchanged_passes(self):
        (self.root / "a.txt").write_text("dirty\n")
        self.stamp()
        self.assertEqual(self.verify(), 0)

    def test_head_moved_fails(self):
        self.stamp()
        self.git("commit", "-q", "--allow-empty", "-m", "y")
        self.assertEqual(self.verify(), 1)

    def test_partial_commit_of_a_dirty_tree_fails(self):
        (self.root / "a.txt").write_text("a2\n")
        (self.root / "b.txt").write_text("b2\n")
        self.stamp()
        self.git("commit", "-q", "-m", "part", "a.txt")
        self.git("reset", "-q", "--soft", self.head)  # HEAD back, porcelain differs
        self.assertEqual(self.verify(), 1)

    def test_untracked_file_created_mid_run_fails(self):
        self.stamp()
        (self.root / "new.txt").write_text("n\n")
        self.assertEqual(self.verify(), 1)

    def test_edit_then_revert_fails(self):
        self.stamp()
        (self.root / "a.txt").write_text("a\n")  # same content, newer mtime
        self.assertEqual(self.verify(), 1)

    def test_checkout_away_and_back_fails(self):
        self.git("branch", "other")
        self.stamp()
        self.git("checkout", "-q", "other")
        self.git("checkout", "-q", "-")
        (self.root / "a.txt").touch()
        self.assertEqual(self.verify(), 1)

    def test_no_stamp_passes(self):
        self.assertEqual(self.verify(), 0)


if __name__ == "__main__":
    unittest.main()
