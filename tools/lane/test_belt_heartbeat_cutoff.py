"""harmonic-forge#1398 / #1591: the afk idle cutoff, now in belt_heartbeat, against a temporary HOME.

Moved from tools/hooks/test_afk_toggle.py with the helpers. afk's state still lives in afk_toggle,
so this file puts tools/hooks on `sys.path` itself.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import afk_toggle  # noqa: E402
import belt_heartbeat  # noqa: E402


class IdleCutoffTests(unittest.TestCase):
    T0 = 1_800_000_000.0  # the afk episode starts here

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def afk_on(self, at=None):
        afk_toggle.toggle({"prompt": "/afk on", "session_id": "s", "cwd": os.getcwd()}, "cli",
                          now=self.T0 if at is None else at)

    def test_nothing_is_due_while_afk_is_off(self):
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 99999))

    def test_not_due_before_an_hour_and_due_at_an_hour(self):
        self.afk_on()
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 3599))
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 3600))

    def test_a_recent_action_pushes_the_cutoff_out(self):
        self.afk_on()
        afk_toggle.record_lane_action("1", now=self.T0 + 3000)
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 3000 + 3599))
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 3000 + 3600))

    def test_an_action_before_afk_went_on_does_not_count(self):
        afk_toggle.record_lane_action("1", now=self.T0 - 100000)
        self.afk_on()
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 3599))

    def test_another_lanes_action_does_not_push_it_out(self):
        self.afk_on()
        afk_toggle.record_lane_action("2", now=self.T0 + 3500)
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 3600))

    def test_announced_once_per_episode_even_across_a_watcher_restart(self):
        self.afk_on()
        since = belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000)
        self.assertTrue(belt_heartbeat.mark_cutoff_announced("l1", since))
        # a new watcher process has no memory; only the marker file remains
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 9000))

    def test_a_new_afk_episode_announces_again(self):
        self.afk_on()
        belt_heartbeat.mark_cutoff_announced("l1", belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000))
        self.afk_on(at=self.T0 + 20000)
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 20000 + 100))
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 20000 + 3600))

    def test_off_then_on_in_the_same_second_is_a_new_episode(self):
        self.afk_on()
        belt_heartbeat.mark_cutoff_announced("l1", belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000))
        afk_toggle.toggle({"prompt": "/afk off", "session_id": "s", "cwd": os.getcwd()}, "cli")
        self.afk_on()                      # the same UTC second, so the same `since` string
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000))

    def test_the_marker_is_per_scope_so_split_sessions_each_get_the_line(self):
        self.afk_on()
        due = belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, scope="o/a")
        self.assertTrue(belt_heartbeat.mark_cutoff_announced("l1", due, scope="o/a"))
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, scope="O/A"))
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, scope="o/b"))
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000))   # the `all` belt

    def test_the_marker_is_per_workspace_so_each_workspaces_belt_gets_the_line(self):
        self.afk_on()
        for workspace in ("vh", "kenekted", "leasepal"):
            due = belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, workspace=workspace)
            self.assertIsNotNone(due, workspace)
            self.assertTrue(belt_heartbeat.mark_cutoff_announced("l1", due, workspace=workspace))
        for workspace in ("vh", "kenekted", "leasepal"):
            self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, workspace=workspace))

    def test_a_marker_that_exists_but_is_unreadable_counts_as_announced(self):
        self.afk_on()
        marker = belt_heartbeat.cutoff_marker_path("l1", "vh", "all")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{not json", encoding="utf-8")
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, workspace="vh"))
        marker.unlink()
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, workspace="vh"))

    def test_a_marker_without_since_counts_as_announced(self):
        self.afk_on()
        marker = belt_heartbeat.cutoff_marker_path("l1", "vh", "all")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        self.assertIsNone(belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000, workspace="vh"))

    def test_a_failed_marker_write_reports_false_so_the_caller_does_not_announce(self):
        self.afk_on()
        with mock.patch.object(afk_toggle, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(belt_heartbeat.mark_cutoff_announced("l1", "x", workspace="vh"))

    def test_the_marker_is_per_lane(self):
        self.afk_on()
        belt_heartbeat.mark_cutoff_announced("l1", belt_heartbeat.idle_cutoff_due("l1", now=self.T0 + 4000))
        self.assertIsNotNone(belt_heartbeat.idle_cutoff_due("l2", now=self.T0 + 4000))

    def test_the_marker_lives_in_afks_state_dir_not_the_heartbeats(self):
        # harmonic-forge#1591: the move must not relocate markers already on disk
        marker = belt_heartbeat.cutoff_marker_path("l1", "vh", "all")
        self.assertEqual(marker.parent, afk_toggle.state_dir())
        self.assertNotEqual(marker.parent, belt_heartbeat.state_dir())


if __name__ == "__main__":
    unittest.main()
