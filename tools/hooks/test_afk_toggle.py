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
                                 "command": command})

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

    def archives(self):
        return sorted(afk_toggle.state_dir().glob("drained-*.jsonl"))

    def test_off_drains_every_workspace(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.queue(self.ws_b, "gh issue close 2")
        self.queue(self.ws_a, "gh issue close 3")
        message = self.typed("/afk off", cwd=self.ws_a)
        for cwd, n in ((self.ws_a, 1), (self.ws_b, 2), (self.ws_a, 3)):
            self.assertIn(f"cd {cwd} && gh issue close {n}", message)
        self.assertEqual(message.count("```"), 2)
        self.assertFalse(afk_toggle.queue_path().exists())

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

if __name__ == "__main__":
    unittest.main()
