"""harmonic-forge#949: scratch checks get disk-backed, locked, reaped, preflighted space."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scratch  # noqa: E402

HERE = Path(__file__).resolve().parent


class ScratchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "root"
        env = mock.patch.dict(os.environ, {"HRSE_SCRATCH_ROOT": str(self.base),
                                           "HRSE_SCRATCH_MIN_FREE_GB": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def _dir(self, name: str, pid: int | None) -> Path:
        path = self.base / name
        path.mkdir(parents=True)
        if pid is not None:
            (path / _scratch.OWNER).write_text(json.dumps({"pid": pid}))
        return path

    def test_dead_owner_is_reaped_live_owner_kept(self) -> None:
        dead = self._dir("dead", self._dead_pid())
        orphan = self._dir("orphan", None)
        live = self._dir("live", os.getpid())
        removed = _scratch.reap(self.base)
        self.assertEqual(removed, [dead])
        self.assertTrue(live.exists())
        # A directory another tool made via TMPDIR (no owner file) is not ours.
        self.assertTrue(orphan.exists())

    def test_nested_run_takes_a_subdir_without_the_lock(self) -> None:
        with _scratch.scratch_dir("outer") as outer:
            with mock.patch.dict(os.environ, {_scratch.HELD_ENV: str(outer),
                                              "HRSE_SCRATCH_LOCK_WAIT_S": "0"}):
                with _scratch.scratch_dir("inner") as inner:
                    self.assertEqual(inner.parent, outer)
                self.assertFalse(inner.exists())

    def test_scratch_dir_reaps_before_creating_and_removes_on_exit(self) -> None:
        dead = self._dir("dead", self._dead_pid())
        with _scratch.scratch_dir("t") as path:
            self.assertFalse(dead.exists())
            self.assertEqual(path.parent, self.base)
            self.assertEqual(json.loads((path / _scratch.OWNER).read_text())["pid"], os.getpid())
        self.assertFalse(path.exists())

    def test_free_space_floor_refuses(self) -> None:
        with mock.patch.dict(os.environ, {"HRSE_SCRATCH_MIN_FREE_GB": str(10**9)}):
            with self.assertRaises(_scratch.ScratchError) as caught:
                with _scratch.scratch_dir("t"):
                    self.fail("must not yield below the floor")
        self.assertIn("below the", str(caught.exception))

    def test_lock_serializes_two_runs(self) -> None:
        self.base.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys, time; sys.path.insert(0, {str(HERE)!r})
            import _scratch
            with _scratch.scratch_dir("holder"):
                print("held", flush=True); time.sleep(3)
            """)], stdout=subprocess.PIPE, text=True,
            env={**os.environ, "HRSE_SCRATCH_ROOT": str(self.base)})
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        with _scratch.scratch_dir("second"):
            waited = time.monotonic() - started
        self.assertGreater(waited, 1.0)

    def test_lock_wait_is_bounded_and_names_the_holder(self) -> None:
        self.base.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys, time; sys.path.insert(0, {str(HERE)!r})
            import _scratch
            with _scratch.scratch_dir("holder-label"):
                print("held", flush=True); time.sleep(30)
            """)], stdout=subprocess.PIPE, text=True,
            env={**os.environ, "HRSE_SCRATCH_ROOT": str(self.base)})
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with mock.patch.dict(os.environ, {"HRSE_SCRATCH_LOCK_WAIT_S": "1"}):
            with self.assertRaises(_scratch.ScratchError) as caught:
                with _scratch.scratch_dir("second"):
                    pass
        self.assertIn("holder-label", str(caught.exception))

    def test_default_root_is_never_under_tmp(self) -> None:
        with mock.patch.dict(os.environ, {"HRSE_SCRATCH_ROOT": ""}):
            self.assertFalse(str(_scratch.root()).startswith("/tmp"))

    def test_call_sites_use_the_helper_not_mkdtemp_on_tmp(self) -> None:
        l1 = (HERE / "l1_post.py").read_text()
        self.assertNotIn('tempfile.mkdtemp(prefix="hrse-l1-post-")', l1)
        self.assertIn("_scratch.scratch_dir(", l1)
        self.assertIn("_scratch.scratch_dir(", (HERE / "kill_check.py").read_text())


if __name__ == "__main__":
    unittest.main()
