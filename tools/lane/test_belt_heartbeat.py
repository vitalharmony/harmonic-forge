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


class OneKeyTests(unittest.TestCase):
    """harmonic-forge#1398 preclose pass 1: the writer and every reader build a belt's identity
    with `belt_heartbeat.key`, so no spelling can miss the file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_lane_spelling_names_the_same_file(self):
        belt_heartbeat.write("l1", "vh", now=1000.0)
        for spelling in ("1", "l1", "L1", " l1 "):
            self.assertEqual(belt_heartbeat.read(spelling, "vh", now=1010.0), 10.0, spelling)

    def test_scope_spellings_agree_and_differ_from_all(self):
        belt_heartbeat.write("l1", "vh", ["B/y", "a/x"], now=1000.0)
        for scope in ("a/x+b/y", "A/X,B/Y", ["b/y", "a/x"]):
            self.assertEqual(belt_heartbeat.read("l1", "vh", scope, now=1005.0), 5.0, scope)
        self.assertIsNone(belt_heartbeat.read("l1", "vh", "all", now=1005.0))

    def test_key_is_lane_workspace_and_scope(self):
        self.assertEqual(belt_heartbeat.key("1", "vh"), "l1-vh-all")
        self.assertEqual(belt_heartbeat.key("L2", "vh", "o/r"), "l2-vh-o%2Fr")
        self.assertNotEqual(belt_heartbeat.key("l1", "vh", "o/a"), belt_heartbeat.key("l1", "vh", "o/b"))
        self.assertNotEqual(belt_heartbeat.key("l1", "vh"), belt_heartbeat.key("l1", "kenekted"))

    def test_distinct_belts_never_share_a_name(self):
        """The escaping is reversible, so these distinct identities cannot collide (the old slug
        mapped `o/repo` and `o/repo_` to one name, and `a-b`/`c` onto `a`/`b-c`)."""
        self.assertNotEqual(belt_heartbeat.key("l1", "vh", "o/repo"), belt_heartbeat.key("l1", "vh", "o/repo_"))
        self.assertNotEqual(belt_heartbeat.key("l1", "a-b", "c/d"), belt_heartbeat.key("l1", "a", "b-c/d"))
        self.assertNotEqual(belt_heartbeat.key("l1", "a b"), belt_heartbeat.key("l1", "a_b"))
        self.assertNotEqual(belt_heartbeat.key("l1", "vh", "a/x"), belt_heartbeat.key("l1", "vh", "a_x"))

    def test_a_long_scope_stays_under_the_name_limit_and_distinct(self):
        owner = "o" * 39
        a = belt_heartbeat.key("l1", "vh", f"{owner}/{'a' * 100}")
        b = belt_heartbeat.key("l1", "vh", f"{owner}/{'a' * 99}b")
        self.assertLessEqual(len(belt_heartbeat.path_for("l1", "vh", f"{owner}/{'a' * 100}").name), 255)
        self.assertNotEqual(a, b)
        belt_heartbeat.write("l1", "vh", f"{owner}/{'a' * 100}", now=1000.0)
        self.assertEqual(belt_heartbeat.read("l1", "vh", f"{owner}/{'a' * 100}", now=1001.0), 1.0)

    def test_spellings_of_one_belt_share_a_name(self):
        base = belt_heartbeat.key("l1", "vh", ["a/b", "c/d"])
        for lane in ("1", "L1", " l1 "):
            self.assertEqual(belt_heartbeat.key(lane, "vh", ["c/d", "A/B"]), base)
        for scope in ("a/b+c/d", "c/d,a/b", ["C/D", "a/b", "a/b"]):
            self.assertEqual(belt_heartbeat.key("l1", "vh", scope), base)

    def test_only_key_builds_heartbeat_and_cutoff_names(self):
        """The one-builder claim, by grep over the shipped sources, not by call counts."""
        root = HERE.parent
        offenders = []
        for path in list(root.rglob("*.py")):
            if path.name.startswith("test_") or path.name == "belt_heartbeat.py" or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for token in ('f"heartbeat-', "f'heartbeat-", 'f"cutoff-', "f'cutoff-"):
                if token in text and path.name != "afk_toggle.py":
                    offenders.append((path.name, token))
        self.assertEqual(offenders, [])

    def test_the_cli_accepts_the_session_lane_and_a_scope(self):
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        belt_heartbeat.write("l1", "vh", "o/r")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(belt_heartbeat.main(["read", "--lane", "1", "--workspace", "vh",
                                                  "--scope", "O/R"]), 0)
            self.assertEqual(belt_heartbeat.main(["read", "--lane", "1", "--workspace", "vh"]), 1)


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
        self.assertEqual(self.watcher.heartbeat_scope(["b/y", "A/x"]), "a/x+b/y")
        # the canonical belt narrows with --scope-repo, a single repo; --repo is a list
        self.assertEqual(self.watcher.heartbeat_scope(None, "a/x"), self.watcher.heartbeat_scope(["a/x"]))

    def test_the_writer_records_a_heartbeat_the_reader_sees(self):
        self.assertTrue(self.watcher.write_poll_heartbeat("l1", "vh", ["a/x"], 1800))
        self.assertIsNotNone(belt_heartbeat.read("l1", "vh", "a/x"))

    def test_the_watchers_heartbeat_never_resets_the_lanes_action_clock(self):
        afk = self.watcher._afk_toggle
        afk.record_lane_action("1", now=1000.0)
        self.watcher.write_poll_heartbeat("l1", "vh", None, None)
        self.assertEqual(afk.last_action_age("1", now=9000.0), 8000.0)

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

    def test_three_workspace_belts_of_one_lane_each_announce_once(self):
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        afk = self.watcher._afk_toggle
        afk.toggle({"prompt": "/afk on", "session_id": "s", "cwd": os.getcwd()}, "cli", now=1.0)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(afk.time, "time", return_value=1.0 + 3700):
            for workspace in ("vh", "kenekted", "leasepal"):
                self.assertTrue(self.watcher.announce_afk_idle_cutoff("l1", "all", workspace), workspace)
            for workspace in ("vh", "kenekted", "leasepal"):   # a second poll of each stays quiet
                self.assertFalse(self.watcher.announce_afk_idle_cutoff("l1", "all", workspace), workspace)
        self.assertEqual(out.getvalue().count("afk-idle-cutoff l1"), 3)

    def test_a_failing_marker_write_announces_nothing_and_never_twice(self):
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        afk = self.watcher._afk_toggle
        afk.toggle({"prompt": "/afk on", "session_id": "s", "cwd": os.getcwd()}, "cli", now=1.0)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(afk.time, "time", return_value=1.0 + 3700), \
                mock.patch.object(afk, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(self.watcher.announce_afk_idle_cutoff("l1", "all", "vh"))
            self.assertFalse(self.watcher.announce_afk_idle_cutoff("l1", "all", "vh"))
        self.assertEqual(out.getvalue(), "")

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

    def _loop_statements(self):
        import ast  # noqa: PLC0415
        tree = ast.parse(Path(self.watcher.__file__).read_text(encoding="utf-8"))
        main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_main")
        loop = next(n for n in ast.walk(main) if isinstance(n, ast.While)
                    and isinstance(n.test, ast.Constant) and n.test.value is True)
        return loop.body

    def _direct_calls(self, name):
        import ast  # noqa: PLC0415
        return [i for i, st in enumerate(self._loop_statements())
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                and getattr(st.value.func, "id", None) == name]

    def test_the_cutoff_check_is_an_unguarded_statement_of_the_poll_loop(self):
        """A call wrapped in `if False:` still counts as 'a call by that name' to a tree walk;
        it must be a direct statement of the loop body to run on every poll."""
        self.assertEqual(len(self._direct_calls("announce_afk_idle_cutoff")), 1)

    def test_the_heartbeat_write_is_an_unguarded_statement_after_the_tick_write(self):
        import ast  # noqa: PLC0415
        body = self._loop_statements()
        tick_write = [i for i, st in enumerate(body) if isinstance(st, ast.Try)
                      and any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "write"
                              and getattr(getattr(n.func, "value", None), "id", None) == "tick"
                              for n in ast.walk(st))]
        writes = self._direct_calls("write_poll_heartbeat")
        self.assertEqual((len(tick_write), len(writes)), (1, 1))
        self.assertGreater(writes[0], tick_write[0])
        self.assertGreater(self._direct_calls("announce_afk_idle_cutoff")[0], writes[0])

    def test_the_poll_loop_passes_the_scope_repo_to_both_the_writer_and_the_cutoff(self):
        import ast  # noqa: PLC0415
        body = self._loop_statements()
        write = body[self._direct_calls("write_poll_heartbeat")[0]].value
        cutoff = body[self._direct_calls("announce_afk_idle_cutoff")[0]].value
        self.assertIn("scope_repo", ast.unparse(write))
        self.assertIn("scope_repo", ast.unparse(cutoff))


if __name__ == "__main__":
    unittest.main()
