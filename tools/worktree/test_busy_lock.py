#!/usr/bin/env python3
"""Tests for the per-worktree busy lock (harmonic-forge#1560).

Every lock here is a real `flock` held by a child bash, in a store under a
temp `WORKTREE_BUSY_DIR`; nothing touches ~/.cache/harmonic-forge/worktree-busy.
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

    def test_nested_check_in_the_same_worktree_proceeds(self):
        repo = self.repo()
        self.run_prelude("check", repo, "sleep 20")
        self.assertTrue(wait_for(lambda: busy_lock.is_busy(repo)))
        inner = self.run_prelude("check", repo, "echo inner-ran")
        _out, err = inner.communicate(timeout=30)
        self.assertEqual(inner.returncode, 0, err)
        self.assertIn("worktree-busy: lock not taken", err)
        self.assertTrue(busy_lock.is_busy(repo), "the outer check still holds the lock")


if __name__ == "__main__":
    unittest.main()
