"""harmonic-forge#1338: the `/afk` toggle (UserPromptSubmit) against a temporary HOME."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import afk_toggle


class ToggleCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws_a = self.home / "ws-a"
        self.ws_b = self.home / "ws-b"
        self.ws_a.mkdir()
        self.ws_b.mkdir()

    def typed(self, prompt, cwd=None, entrypoint="cli"):
        return afk_toggle.toggle({"prompt": prompt, "session_id": "s", "cwd": str(cwd or self.ws_a)},
                                 entrypoint)

    def queue(self, cwd, command):
        afk_toggle.append_queue({"cwd": str(cwd), "workspace": afk_toggle.workspace_of(str(cwd)),
                                 "command": command, "ts": "2026-11-12T00:00:00Z"})

    def test_on_status_off(self):
        self.assertIn("ON", self.typed("/afk on"))
        self.assertTrue(afk_toggle.state_path().exists())
        self.assertIn("ON since", self.typed("  /afk status "))
        self.assertIn("ON since", self.typed("/afk"))
        self.assertIn("OFF", self.typed("/afk off"))
        self.assertFalse(afk_toggle.state_path().exists())

    def test_prose_around_the_command_changes_nothing(self):
        for prompt in ("please /afk on now", "/afk on please", "I typed /afk on", "/afk onn"):
            self.typed(prompt)
            self.assertFalse(afk_toggle.state_path().exists(), prompt)
        self.typed("/afk on")
        for prompt in ("then /afk off", "/afk off later"):
            self.typed(prompt)
            self.assertTrue(afk_toggle.state_path().exists(), prompt)

    def test_non_interactive_entrypoint_cannot_turn_on(self):
        self.assertIn("nothing changed", self.typed("/afk on", entrypoint="sdk-py"))
        self.assertFalse(afk_toggle.state_path().exists())

    def test_off_honored_from_non_interactive(self):
        self.typed("/afk on")
        self.typed("/afk off", entrypoint=None)
        self.assertFalse(afk_toggle.state_path().exists())

    def test_headless_off_leaves_queue_for_interactive_off(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 7")
        message = self.typed("/afk off", entrypoint="sdk-py")
        self.assertIn("OFF", message)
        self.assertIn("interactive `/afk off`", message)
        self.assertNotIn("gh issue close 7", message)
        self.assertFalse(afk_toggle.state_path().exists())
        self.assertTrue(afk_toggle.queue_path().exists())
        self.assertEqual(self.archives(), [])
        later = self.typed("/afk off")
        self.assertIn(f"1. time: 2026-11-12T00:00:00Z; cwd: {json.dumps(str(self.ws_a))}; "
                      f"command: {json.dumps('gh issue close 7')}", later)
        self.assertFalse(afk_toggle.queue_path().exists())

    def archives(self):
        return sorted(afk_toggle.state_dir().glob("drained-*.jsonl"))

    def test_off_drains_every_workspace(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.queue(self.ws_b, "gh issue close 2")
        self.queue(self.ws_a, "gh issue close 3")
        message = self.typed("/afk off", cwd=self.ws_a)
        for i, (cwd, n) in enumerate(((self.ws_a, 1), (self.ws_b, 2), (self.ws_a, 3)), 1):
            self.assertIn(f"{i}. time: 2026-11-12T00:00:00Z; cwd: {json.dumps(str(cwd))}; "
                          f"command: {json.dumps(f'gh issue close {n}')}", message)
        self.assertNotIn("```", message)
        self.assertNotIn("cd ", message)
        self.assertIn(str(self.archives()[0]), message)
        self.assertTrue(message.endswith(afk_toggle.RERUN_SENTENCE))
        self.assertIn("Ask the session to re-run any of these; do not paste them into a "
                      "terminal (hooks would not run).", message)
        self.assertFalse(afk_toggle.queue_path().exists())

    def test_hostile_command_listed_intact_as_json(self):
        self.typed("/afk on")
        command = 'gh issue close 7 --comment "done `rm -rf ~`"\necho \'x\''
        self.queue(self.ws_a, command)
        message = self.typed("/afk off")
        self.assertIn(f"command: {json.dumps(command)}", message)
        self.assertNotIn(command, message)
        self.assertNotIn("cd ", message)
        self.assertNotIn("```", message)
        self.assertTrue(message.endswith(afk_toggle.RERUN_SENTENCE))

    def test_append_blocked_on_off_lock_writes_nothing(self):
        """The append passed its outer is_on() check, then blocks on the lock
        `_off` holds; it must see afk off, write nothing, and so the caller
        never denies: the command is never lost."""
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.assertTrue(afk_toggle.is_on())  # the outer check the append passed
        real_unlink = Path.unlink
        worker, results = [], []

        def racing_unlink(path, *a, **kw):
            if path == afk_toggle.state_path() and not worker:
                t = threading.Thread(target=lambda: results.append(afk_toggle.append_queue(
                    {"cwd": str(self.ws_b), "command": "gh issue close 9"})))
                t.start()
                worker.append(t)
                time.sleep(0.3)
            return real_unlink(path, *a, **kw)

        with mock.patch.object(Path, "unlink", racing_unlink):
            message = self.typed("/afk off")
        worker[0].join(5)
        self.assertEqual(results, [False])  # wrote nothing
        self.assertNotIn("gh issue close 9", message)
        self.assertFalse(afk_toggle.queue_path().exists())
        archived = "".join(a.read_text() for a in self.archives())
        self.assertNotIn("gh issue close 9", archived)
        # Never lost: False means afk_defer returns no deny, so batch_auth asks.

    def test_second_off_prints_nothing(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.typed("/afk off")
        again = self.typed("/afk off", cwd=self.ws_b)
        self.assertNotIn("gh issue close", again)
        self.assertNotIn("```", again)
        self.assertIn("OFF", again)

    def test_unreadable_archive_keeps_file_and_reports_path(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        with mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            message = self.typed("/afk off")
        archives = self.archives()
        self.assertEqual(len(archives), 1)
        self.assertIn(str(archives[0]), message)
        self.assertIn("afk is OFF", message)
        self.assertNotIn("nothing changed", message)
        self.assertFalse(afk_toggle.state_path().exists())
        self.assertIn("gh issue close 1", archives[0].read_text())

    def test_append_during_drain_is_kept(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        real_replace = os.replace
        worker, results = [], []

        def racing_replace(src, dst):
            t = threading.Thread(target=lambda: results.append(afk_toggle.append_queue(
                {"cwd": str(self.ws_b), "command": "gh issue close 9"})))
            t.start()
            worker.append(t)
            time.sleep(0.3)
            return real_replace(src, dst)

        with mock.patch.object(afk_toggle.os, "replace", racing_replace):
            message = self.typed("/afk off")
        worker[0].join(5)
        self.assertIn("gh issue close 1", message)
        # The racing append waited on the lock, saw afk off, and wrote nothing:
        # no line is lost silently, none is printed twice.
        self.assertEqual(results, [False])
        self.assertFalse(afk_toggle.queue_path().exists())

    def test_append_writes_nothing_once_off(self):
        self.assertFalse(afk_toggle.append_queue({"command": "gh issue close 1"}))
        self.assertFalse(afk_toggle.queue_path().exists())

    def test_drained_lines_are_kept_in_archive(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.queue(self.ws_b, "gh issue close 2")
        self.typed("/afk off", cwd=self.ws_a)
        kept = [json.loads(l)["command"] for l in self.archives()[0].read_text().splitlines()]
        self.assertEqual(kept, ["gh issue close 1", "gh issue close 2"])


class LastActionCase(unittest.TestCase):
    """harmonic-forge#1398: one record per lane, written only by a posting tool."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_record_reads_back_as_its_age(self):
        self.assertTrue(afk_toggle.record_lane_action("1", now=1000.0))
        self.assertEqual(afk_toggle.last_action_age("1", now=1090.0), 90.0)

    def test_lane_spellings_agree(self):
        for spelling in ("1", "l1", "L1"):
            afk_toggle.record_lane_action(spelling, now=2000.0)
            self.assertEqual(afk_toggle.last_action_age("l1", now=2001.0), 1.0)

    def test_a_lane_2_post_never_resets_lane_1(self):
        afk_toggle.record_lane_action("1", now=1000.0)
        afk_toggle.record_lane_action("2", now=5000.0)
        self.assertEqual(afk_toggle.last_action_age("1", now=5000.0), 4000.0)
        self.assertEqual(afk_toggle.last_action_age("2", now=5000.0), 0.0)

    def test_an_explicit_lane_does_not_depend_on_the_environment(self):
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(afk_toggle.record_lane_action("2", now=10.0))
        with mock.patch.dict(os.environ, {"LANE": "1"}):
            afk_toggle.record_lane_action("2", now=20.0)
        self.assertEqual(afk_toggle.last_action_age("2", now=21.0), 1.0)
        self.assertIsNone(afk_toggle.last_action_age("1", now=21.0))

    def test_the_lane_comes_from_the_environment_by_default(self):
        with mock.patch.dict(os.environ, {"LANE": "1"}):
            self.assertTrue(afk_toggle.record_lane_action(now=10.0))
        self.assertIsNotNone(afk_toggle.last_action_age("1", now=11.0))

    def test_a_session_with_no_lane_records_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(afk_toggle.record_lane_action())
        self.assertFalse(list(afk_toggle.state_dir().glob("last_action-*")) if afk_toggle.state_dir().exists() else [])

    def test_an_unknown_lane_records_nothing(self):
        self.assertFalse(afk_toggle.record_lane_action("4"))
        self.assertFalse(afk_toggle.record_lane_action("x"))

    def test_no_record_future_or_garbled_reads_as_none(self):
        self.assertIsNone(afk_toggle.last_action_age("1"))
        afk_toggle.record_lane_action("1", now=9000.0)
        self.assertIsNone(afk_toggle.last_action_age("1", now=100.0))
        afk_toggle.last_action_path("1").write_text("garbled")
        self.assertIsNone(afk_toggle.last_action_age("1", now=9500.0))

    def test_a_failed_write_never_raises(self):
        with mock.patch.object(afk_toggle, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(afk_toggle.record_lane_action("1"))

    def test_the_belt_heartbeat_never_writes_a_lane_action(self):
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lane"))
        import belt_heartbeat  # noqa: PLC0415
        belt_heartbeat.write("l1", "vh")
        self.assertIsNone(afk_toggle.last_action_age("1"))

    # -- the idle cutoff: wall time from the later of the last action and `/afk on`
    T0 = 1_800_000_000.0  # the afk episode starts here

    def afk_on(self, at=None):
        afk_toggle.toggle({"prompt": "/afk on", "session_id": "s", "cwd": os.getcwd()}, "cli",
                          now=self.T0 if at is None else at)

    def test_nothing_is_due_while_afk_is_off(self):
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 99999))

    def test_not_due_before_an_hour_and_due_at_an_hour(self):
        self.afk_on()
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 3599))
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 3600))

    def test_a_recent_action_pushes_the_cutoff_out(self):
        self.afk_on()
        afk_toggle.record_lane_action("1", now=self.T0 + 3000)
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 3000 + 3599))
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 3000 + 3600))

    def test_an_action_before_afk_went_on_does_not_count(self):
        afk_toggle.record_lane_action("1", now=self.T0 - 100000)
        self.afk_on()
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 3599))

    def test_another_lanes_action_does_not_push_it_out(self):
        self.afk_on()
        afk_toggle.record_lane_action("2", now=self.T0 + 3500)
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 3600))

    def test_announced_once_per_episode_even_across_a_watcher_restart(self):
        self.afk_on()
        since = afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000)
        self.assertTrue(afk_toggle.mark_cutoff_announced("l1", since))
        # a new watcher process has no memory; only the marker file remains
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 9000))

    def test_a_new_afk_episode_announces_again(self):
        self.afk_on()
        afk_toggle.mark_cutoff_announced("l1", afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000))
        self.afk_on(at=self.T0 + 20000)
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 20000 + 100))
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 20000 + 3600))

    def test_off_then_on_in_the_same_second_is_a_new_episode(self):
        self.afk_on()
        afk_toggle.mark_cutoff_announced("l1", afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000))
        afk_toggle.toggle({"prompt": "/afk off", "session_id": "s", "cwd": os.getcwd()}, "cli")
        self.afk_on()                      # the same UTC second, so the same `since` string
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000))

    def test_the_marker_is_per_scope_so_split_sessions_each_get_the_line(self):
        self.afk_on()
        due = afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, scope="o/a")
        self.assertTrue(afk_toggle.mark_cutoff_announced("l1", due, scope="o/a"))
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, scope="O/A"))
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, scope="o/b"))
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000))   # the `all` belt

    def test_the_marker_is_per_workspace_so_each_workspaces_belt_gets_the_line(self):
        self.afk_on()
        for workspace in ("vh", "kenekted", "leasepal"):
            due = afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, workspace=workspace)
            self.assertIsNotNone(due, workspace)
            self.assertTrue(afk_toggle.mark_cutoff_announced("l1", due, workspace=workspace))
        for workspace in ("vh", "kenekted", "leasepal"):
            self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, workspace=workspace))

    def test_a_marker_that_exists_but_is_unreadable_counts_as_announced(self):
        self.afk_on()
        marker = afk_toggle.cutoff_marker_path("l1", "vh", "all")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{not json", encoding="utf-8")
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, workspace="vh"))
        marker.unlink()
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, workspace="vh"))

    def test_a_marker_without_since_counts_as_announced(self):
        self.afk_on()
        marker = afk_toggle.cutoff_marker_path("l1", "vh", "all")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        self.assertIsNone(afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000, workspace="vh"))

    def test_a_closed_stderr_never_makes_the_record_raise(self):
        with mock.patch.object(afk_toggle, "_atomic_write", side_effect=OSError("disk full")), \
                mock.patch("builtins.print", side_effect=ValueError("I/O operation on closed file")):
            self.assertFalse(afk_toggle.record_lane_action("1"))

    def test_a_failed_marker_write_reports_false_so_the_caller_does_not_announce(self):
        self.afk_on()
        with mock.patch.object(afk_toggle, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(afk_toggle.mark_cutoff_announced("l1", "x", workspace="vh"))

    def test_the_marker_is_per_lane(self):
        self.afk_on()
        afk_toggle.mark_cutoff_announced("l1", afk_toggle.idle_cutoff_due("l1", now=self.T0 + 4000))
        self.assertIsNotNone(afk_toggle.idle_cutoff_due("l2", now=self.T0 + 4000))


if __name__ == "__main__":
    unittest.main()
