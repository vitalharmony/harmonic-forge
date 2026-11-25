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
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import busy_lock  # noqa: E402

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

    def test_fd_holders_leaves_out_the_caller_and_its_parent(self):
        lock = busy_lock.lock_path(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "w"):
            self.assertNotIn(os.getpid(), busy_lock.fd_holders(lock))
        probe = self.base / "probe.py"
        probe.write_text("import pathlib, sys\n"
                         f"sys.path.insert(0, {str(HERE)!r})\n"
                         "import busy_lock\n"
                         "print(busy_lock.fd_holders(pathlib.Path(sys.argv[1])))\n")
        done = subprocess.run(["bash", "-c", f"exec 9>{shlex.quote(str(lock))}; "
                               f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))} "
                               f"{shlex.quote(str(lock))}; true"],
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "[]"),
                         f"neither the caller nor its parent shell is a holder: {done.stderr}")

    def test_begin_holds_the_lock_on_the_inherited_fd_after_it_exits(self):
        """Revision 2: the task shell opens fd 9, `begin --fd 9` locks it, and
        the lock outlives `begin` because it belongs to the open file description."""
        lock = busy_lock.lock_path(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        script = (f"exec 9>{shlex.quote(str(lock))}; "
                  f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} begin "
                  f"--root {shlex.quote(str(self.root))} --fd 9 --pid $$; sleep 30")
        child = subprocess.Popen(["bash", "-c", script], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, child)
        self.assertTrue(wait_for(lambda: busy_lock.is_busy(self.root)), "never busy")
        self.assertEqual(busy_lock.read_sidecar(self.root)["shell_pid"], child.pid)
        second = subprocess.run(["bash", "-c", f"exec 9>{shlex.quote(str(lock))}; "
                                 f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} begin "
                                 f"--root {shlex.quote(str(self.root))} --fd 9 --pid $$"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertEqual(second.stdout, "", "a refused begin prints no nonce")
        self.assertIn(f"kill -- -{child.pid}", second.stderr)

    def test_begin_with_an_unusable_fd_fails_open_and_still_stamps(self):
        """AC7: the stamp is written on the fail-open path too."""
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for fd in ("", "notanfd", "87"):
            with self.subTest(fd=fd):
                out = subprocess.run([sys.executable, str(SCRIPT), "begin", "--root", str(self.root),
                                      "--fd", fd, "--pid", str(os.getpid())],
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn("lock not taken", out.stderr)
                self.assertFalse(busy_lock.is_busy(self.root))
                self.assertRegex(out.stdout, r"^nonce=[0-9a-f]{16}\n$")
                self.assertTrue(busy_lock.stamp_path(self.root).exists())
                busy_lock.stamp_path(self.root).unlink()



if __name__ == "__main__":
    unittest.main()
