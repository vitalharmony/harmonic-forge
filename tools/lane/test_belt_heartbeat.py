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

    def test_the_heartbeat_is_not_a_lane_action(self):
        """It lives in the belt directory and never touches the afk state, so a
        poll can never reset the afk idle clock."""
        belt_heartbeat.write("l1", "vh", now=1.0)
        self.assertFalse((Path(self._tmp.name) / ".claude" / "state" / "afk").exists())


if __name__ == "__main__":
    unittest.main()
