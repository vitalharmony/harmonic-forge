#!/usr/bin/env python3
"""The busy lock and integrity step as `mise run check` and `local-check`
actually run them (harmonic-forge#1560, sticky-wicket fix C).

Each test executes a task's `run` string from mise.toml unmodified, under
`sh -o errexit -c` as mise does, in a temp git repo. The repo holds a stub at
every other `tools/...` path the string calls, plus the real `busy_lock.py`,
`busy_stamp.py` and `check_receipt.py`. HOME, `WORKTREE_BUSY_DIR` and the
receipt store are temp; `claude` is off PATH. A stub step runs `$STUB_STEP`.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import busy_lock  # noqa: E402

TASKS = ("check", "local-check")
REAL = ("tools/worktree/busy_lock.py", "tools/worktree/busy_stamp.py", "tools/hooks/check_receipt.py")
STEP_AT = "tools/rules/check_cross_registry.py"  # a step both tasks run
STUB = ("import os, subprocess, sys\n"
        "if os.environ.get('STUB_STEP') and __file__.endswith(os.environ.get('STUB_AT') or {at!r}):\n"
        "    sys.exit(subprocess.run(os.environ['STUB_STEP'], shell=True).returncode)\n")


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def run_string(task: str) -> str:
    return tomllib.loads((CHECKOUT / "mise.toml").read_text(encoding="utf-8"))["tasks"][task]["run"]


def final_step(task: str) -> str:
    """The script of the task's last check step: the last line calling a
    `tools/...py` other than the lock, integrity and receipt scripts. In the
    shipped order it is the step immediately preceding `end`."""
    steps = [path for line in run_string(task).splitlines() if not line.lstrip().startswith("#")
             for path in re.findall(r"tools/[\w/.-]+\.py", line) if path not in REAL]
    return steps[-1]


class RunString(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.store = self.base / "store"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        paths = set()
        for task in TASKS:
            paths |= set(re.findall(r"tools/[\w/.-]+\.py", run_string(task)))
        self.assertIn(STEP_AT, paths)
        for rel in paths | set(REAL):
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel in REAL:
                shutil.copy(CHECKOUT / rel, target)
            else:
                target.write_text(STUB.format(at=STEP_AT))
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        (self.repo / "a.txt").write_text("a\n")
        git = ["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run([*git, "remote", "add", "origin", "https://github.com/t/t.git"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
        self.head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                                   text=True).stdout.strip()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        path = [d for d in os.environ.get("PATH", "").split(os.pathsep)
                if d and not os.path.exists(os.path.join(d, "claude"))]
        self.env = {"PATH": os.pathsep.join([str(bin_dir), *path]), "HOME": str(self.base / "home"),
                    "WORKTREE_BUSY_DIR": str(self.store), "PYTHONDONTWRITEBYTECODE": "1",
                    "LANG": "C.UTF-8"}
        os.environ["WORKTREE_BUSY_DIR"] = str(self.store)  # for busy_lock calls in this process
        self.addCleanup(os.environ.pop, "WORKTREE_BUSY_DIR", None)

    def receipt(self) -> Path:
        return self.base / "home" / ".cache" / "harmonic-forge" / "check-receipts" / "t__t" / self.head

    def start(self, task: str, step: str = "", at: str = "") -> subprocess.Popen:
        env = dict(self.env, STUB_STEP=step, STUB_AT=at, RUN=run_string(task))
        child = subprocess.Popen(["sh", "-o", "errexit", "-c", run_string(task)], cwd=self.repo, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate()
        self.addCleanup(stop)
        return child

    def run_task(self, task: str, step: str = "", at: str = "") -> tuple[int, str]:
        child = self.start(task, step, at)
        _out, err = child.communicate(timeout=120)
        return child.returncode, err

    def each_task(self):
        for task in TASKS:
            with self.subTest(task=task):
                self.receipt().unlink(missing_ok=True)
                yield task


class Outcomes(RunString):
    def test_an_unchanged_tree_records_a_receipt(self):
        for task in self.each_task():
            rc, err = self.run_task(task)
            self.assertEqual(rc, 0, err)
            self.assertTrue(self.receipt().exists(), err)

    def test_an_edit_restored_mid_run_fails_with_no_receipt(self):
        for task in self.each_task():
            rc, err = self.run_task(task, "echo b > a.txt; echo a > a.txt")
            self.assertNotEqual(rc, 0, err)
            self.assertIn("changed during check", err)
            self.assertFalse(self.receipt().exists())

    def test_an_edit_at_the_final_step_fails_with_no_receipt(self):
        """Post-verdict gap: `end` must follow the last check step, so an edit
        made inside that step is still inside the integrity window."""
        for task in self.each_task():
            rc, err = self.run_task(task, "echo b > a.txt; echo a > a.txt", at=final_step(task))
            self.assertNotEqual(rc, 0, err)
            self.assertIn("changed during check", err)
            self.assertFalse(self.receipt().exists())

    def test_a_stamp_removed_mid_run_fails(self):
        for task in self.each_task():
            rc, err = self.run_task(task, f"rm {shlex.quote(str(busy_lock.stamp_path(self.repo)))}")
            self.assertNotEqual(rc, 0, err)
            self.assertIn("missing or unreadable", err)
            self.assertFalse(self.receipt().exists())

    def test_a_read_only_store_passes_with_no_receipt(self):
        """Fix F, unverifiable: no lock, no stamp, the check exits 0, no receipt,
        even when the tree changed during the run."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.store.mkdir()
        os.chmod(self.store, 0o500)
        self.addCleanup(os.chmod, self.store, 0o700)
        for task in self.each_task():
            for step in ("", "echo b > a.txt; echo a > a.txt"):
                rc, err = self.run_task(task, step)
                self.assertEqual(rc, 0, err)
                self.assertIn("integrity stamp unavailable", err)
                self.assertFalse(self.receipt().exists(), err)

    def test_a_failed_stamp_under_a_held_lock_passes_with_no_receipt(self):
        for task in self.each_task():
            busy_lock.stamp_path(self.repo).mkdir(parents=True, exist_ok=True)  # os.replace onto a dir fails
            rc, err = self.run_task(task)
            self.assertEqual(rc, 0, err)
            self.assertIn("integrity stamp unavailable", err)
            self.assertNotIn("lock not taken", err)
            self.assertFalse(self.receipt().exists(), err)


class FailOpenLock(RunString):
    """No lock fd (the lock path is a directory) but a writable store: the stamp
    is still written and still verified (AC7, pass-2 #3)."""

    def setUp(self):
        super().setUp()
        busy_lock.lock_path(self.repo).mkdir(parents=True)

    def test_unchanged_records_a_receipt(self):
        for task in self.each_task():
            rc, err = self.run_task(task)
            self.assertEqual(rc, 0, err)
            self.assertIn("lock not taken", err)
            self.assertTrue(self.receipt().exists(), err)

    def test_an_edit_restored_mid_run_still_fails(self):
        for task in self.each_task():
            rc, err = self.run_task(task, "echo b > a.txt; echo a > a.txt")
            self.assertNotEqual(rc, 0, err)
            self.assertIn("changed during check", err)

    def test_a_second_run_overwriting_the_stamp_fails_the_first(self):
        """Fix F: the second run's stamp carries its own nonce."""
        for task in self.each_task():
            rc, err = self.run_task(task, 'STUB_STEP= sh -o errexit -c "$RUN" 2>/dev/null')
            self.assertNotEqual(rc, 0, err)
            self.assertIn("not this run's", err)


class Lock(RunString):
    def test_the_task_holds_the_lock_while_it_runs(self):
        """Busy is read while a later step runs, after `begin` has exited, so
        only a lock on the task shell's own fd 9 can hold it."""
        for task in self.each_task():
            running = self.base / f"running-{task}"
            child = self.start(task, f"touch {running}; sleep 3")
            self.assertTrue(wait_for(running.exists), "the step never ran")
            self.assertTrue(busy_lock.is_busy(self.repo), "not busy while a step runs")
            self.assertEqual(busy_lock.read_sidecar(self.repo)["shell_pid"], child.pid)
            child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0)
            self.assertFalse(busy_lock.is_busy(self.repo))

    def test_a_second_run_refuses_and_the_holder_still_verifies(self):
        """AC6 + AC7 order: the refused second run never touches the holder's
        stamp, so the holder records its receipt."""
        for task in self.each_task():
            nested = self.base / f"nested-{task}.err"
            child = self.start(task, f'STUB_STEP= sh -o errexit -c "$RUN" 2>{nested} && exit 9 || true')
            _out, err = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, err)
            self.assertTrue(self.receipt().exists(), err)
            self.assertIn("already running", nested.read_text())
            self.assertIn(f"kill -- -{child.pid}", nested.read_text())

    def test_a_leaked_fd_refuses_naming_its_holder(self):
        lock = busy_lock.lock_path(self.repo)
        lock.parent.mkdir(parents=True, exist_ok=True)
        leaked = subprocess.Popen(["bash", "-c", f"exec 9>{shlex.quote(str(lock))}; flock 9; sleep 60"])
        self.addCleanup(lambda: (leaked.kill(), leaked.wait()))
        self.assertTrue(wait_for(lambda: busy_lock._lock_held(lock)))
        for task in self.each_task():
            child = self.start(task, "echo ran > ../ran")
            _out, err = child.communicate(timeout=60)
            self.assertNotEqual(child.returncode, 0, err)
            self.assertIn("leaked fd", err)
            holders = [p.strip() for p in err.split("holders:", 1)[1].split(".", 1)[0].split(",")]
            self.assertIn(str(leaked.pid), holders)
            self.assertNotIn(str(child.pid), holders, "the refusing task shell is not a holder")
            self.assertFalse((self.base / "ran").exists(), "a refused check runs no step")


if __name__ == "__main__":
    unittest.main()
