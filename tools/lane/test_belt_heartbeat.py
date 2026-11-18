"""harmonic-forge#1398 -- the belt heartbeat: written by the watcher, read by a tick."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import belt_heartbeat  # noqa: E402


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_heartbeat_reads_as_none_and_stale(self):
        self.assertIsNone(belt_heartbeat.read("l1", "vh"))
        self.assertTrue(belt_heartbeat.is_stale("l1", "vh"))

    def test_a_fresh_write_reads_young_and_not_stale(self):
        belt_heartbeat.write("l1", "vh", now=1000.0)
        self.assertEqual(belt_heartbeat.read("l1", "vh", now=1030.0), 30.0)
        self.assertFalse(belt_heartbeat.is_stale("l1", "vh", now=1030.0))

    def test_older_than_two_intervals_is_stale(self):
        belt_heartbeat.write("l1", "vh", now=1000.0)
        self.assertFalse(belt_heartbeat.is_stale("l1", "vh", max_age=600, now=1600.0))
        self.assertTrue(belt_heartbeat.is_stale("l1", "vh", max_age=600, now=1601.0))

    def test_each_lane_workspace_and_scope_has_its_own_file(self):
        belt_heartbeat.write("l1", "vh", now=1000.0)
        self.assertIsNone(belt_heartbeat.read("l2", "vh", now=1001.0))
        self.assertIsNone(belt_heartbeat.read("l1", "kenekted", now=1001.0))
        self.assertIsNone(belt_heartbeat.read("l1", "vh", "vitalharmony/hrse", now=1001.0))
        belt_heartbeat.write("l1", "vh", "vitalharmony/hrse", now=1000.0)
        self.assertEqual(belt_heartbeat.read("l1", "vh", "vitalharmony/hrse", now=1005.0), 5.0)

    def test_a_scope_with_path_characters_stays_inside_the_state_dir(self):
        path = belt_heartbeat.write("l1", "vh", "../../etc/x", now=1.0)
        self.assertEqual(path.parent, belt_heartbeat.state_dir())

    def test_unparseable_or_future_dated_files_read_as_no_heartbeat(self):
        path = belt_heartbeat.write("l1", "vh", now=1000.0)
        path.write_text("not json")
        self.assertIsNone(belt_heartbeat.read("l1", "vh", now=1001.0))
        path.write_text(json.dumps({"written_at": 5000.0}))
        self.assertIsNone(belt_heartbeat.read("l1", "vh", now=1001.0))
        path.write_text(json.dumps({"other": 1}))
        self.assertIsNone(belt_heartbeat.read("l1", "vh", now=1001.0))

    def test_the_write_is_atomic_and_leaves_no_temp_file(self):
        belt_heartbeat.write("l1", "vh", now=1.0)
        belt_heartbeat.write("l1", "vh", now=2.0)
        names = sorted(p.name for p in belt_heartbeat.state_dir().iterdir())
        self.assertEqual(names, ["heartbeat-l1-vh-all.json"])

    def test_the_cli_exits_0_when_fresh_and_1_when_stale_or_missing(self):
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        argv = ["read", "--lane", "l1", "--workspace", "vh"]
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(belt_heartbeat.main(argv), 1)
        self.assertEqual(out.getvalue().strip(), "none")
        belt_heartbeat.write("l1", "vh")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(belt_heartbeat.main(argv), 0)
            self.assertEqual(belt_heartbeat.main(argv + ["--max-age", "-1"]), 1)

    def test_the_heartbeat_is_not_a_lane_action(self):
        """It lives in the belt directory and never touches the afk state, so a
        poll can never reset the afk idle clock."""
        belt_heartbeat.write("l1", "vh", now=1.0)
        self.assertFalse((Path(self._tmp.name) / ".claude" / "state" / "afk").exists())


class WatcherWritesItTests(unittest.TestCase):
    """The watcher is the one writer. Its `while True:` loop has no seam a test
    can call (the module's own docstring says so), so the loop's call is checked
    on the syntax tree and the writer itself is called directly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.path.insert(0, str(HERE.parent / "gh"))
        import watch_lane_posts  # noqa: PLC0415
        self.watcher = watch_lane_posts

    def test_scope_is_the_sorted_repos_or_all(self):
        self.assertEqual(self.watcher.heartbeat_scope(None), "all")
        self.assertEqual(self.watcher.heartbeat_scope([]), "all")
        self.assertEqual(self.watcher.heartbeat_scope(["b/y", "a/x"]), "a/x+b/y")

    def test_the_writer_records_a_heartbeat_the_reader_sees(self):
        self.assertTrue(self.watcher.write_poll_heartbeat("l1", "vh", ["a/x"], 1800))
        self.assertIsNotNone(belt_heartbeat.read("l1", "vh", "a/x"))

    def test_a_workspaceless_belt_still_writes(self):
        self.assertTrue(self.watcher.write_poll_heartbeat("l1", None, None, None))
        self.assertIsNotNone(belt_heartbeat.read("l1", "none", "all"))

    def test_a_failed_write_never_raises(self):
        with mock.patch.object(belt_heartbeat, "write", side_effect=OSError("disk full")):
            self.assertFalse(self.watcher.write_poll_heartbeat("l1", "vh", None, None))

    def test_the_serving_lane_is_the_session_lane_then_queue_for(self):
        with mock.patch.dict(os.environ, {"LANE": "3"}):
            self.assertEqual(self.watcher.belt_lane(None), "l3")
            self.assertEqual(self.watcher.belt_lane("l1"), "l3")
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.watcher.belt_lane("l2"), "l2")
            self.assertEqual(self.watcher.belt_lane(None), "none")

    def test_the_cutoff_line_prints_once_per_episode_and_survives_a_restart(self):
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        afk = self.watcher._afk_toggle
        afk.toggle({"prompt": "/afk on", "session_id": "s", "cwd": os.getcwd()}, "cli", now=1.0)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(afk.time, "time", return_value=1.0 + 3700):
            self.assertTrue(self.watcher.announce_afk_idle_cutoff("l1"))
            # a restarted watcher has no memory, only the marker file
            self.assertFalse(self.watcher.announce_afk_idle_cutoff("l1"))
        self.assertEqual(out.getvalue().count("afk-idle-cutoff l1"), 1)

    def test_no_cutoff_line_before_the_hour_or_with_afk_off(self):
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        afk = self.watcher._afk_toggle
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(self.watcher.announce_afk_idle_cutoff("l1"))
            afk.toggle({"prompt": "/afk on", "session_id": "s", "cwd": os.getcwd()}, "cli", now=1.0)
            with mock.patch.object(afk.time, "time", return_value=1.0 + 600):
                self.assertFalse(self.watcher.announce_afk_idle_cutoff("l1"))
        self.assertEqual(out.getvalue(), "")

    def test_the_poll_loop_calls_the_cutoff_check_once_per_poll(self):
        import ast  # noqa: PLC0415
        tree = ast.parse(Path(self.watcher.__file__).read_text(encoding="utf-8"))
        main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_main")
        loop = next(n for n in ast.walk(main) if isinstance(n, ast.While)
                    and isinstance(n.test, ast.Constant) and n.test.value is True)
        calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "announce_afk_idle_cutoff"]
        self.assertEqual(len(calls), 1)

    def test_the_poll_loop_calls_the_writer_after_the_tick_write(self):
        import ast  # noqa: PLC0415
        tree = ast.parse(Path(self.watcher.__file__).read_text(encoding="utf-8"))
        main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_main")
        loop = next(n for n in ast.walk(main) if isinstance(n, ast.While)
                    and isinstance(n.test, ast.Constant) and n.test.value is True)
        calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "write_poll_heartbeat"]
        self.assertEqual(len(calls), 1, "the poll loop must write the heartbeat exactly once per poll")


if __name__ == "__main__":
    unittest.main()
