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

    def test_off_drains_only_this_workspace(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.queue(self.ws_b, "gh issue close 2")
        self.queue(self.ws_a, "gh issue close 3")
        message = self.typed("/afk off", cwd=self.ws_a)
        self.assertIn(f"cd {self.ws_a} && gh issue close 1", message)
        self.assertIn(f"cd {self.ws_a} && gh issue close 3", message)
        self.assertEqual(message.count("```"), 2)
        self.assertNotIn("gh issue close 2", message)
        left = [json.loads(l) for l in afk_toggle.queue_path().read_text().splitlines()]
        self.assertEqual([e["command"] for e in left], ["gh issue close 2"])
        self.assertNotIn("gh issue close", self.typed("/afk off", cwd=self.ws_a))

    def test_append_during_drain_is_kept(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        real_read = afk_toggle._read_queue
        worker = []

        def racing_read():
            lines = real_read()
            t = threading.Thread(target=self.queue, args=(self.ws_b, "gh issue close 9"))
            t.start()
            worker.append(t)
            time.sleep(0.3)
            return lines

        with mock.patch.object(afk_toggle, "_read_queue", racing_read):
            drained = afk_toggle.drain(afk_toggle.workspace_of(str(self.ws_a)))
        worker[0].join(5)
        self.assertEqual([e["command"] for e in drained], ["gh issue close 1"])
        left = [json.loads(l)["command"] for l in afk_toggle.queue_path().read_text().splitlines()]
        self.assertEqual(left, ["gh issue close 9"])

    def test_append_writes_nothing_once_off(self):
        self.assertFalse(afk_toggle.append_queue({"command": "gh issue close 1"}))
        self.assertFalse(afk_toggle.queue_path().exists())

    def test_off_lists_other_workspaces_without_draining_them(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.queue(self.ws_b, "gh issue close 2")
        self.queue(self.ws_b, "gh issue close 3")
        message = self.typed("/afk off", cwd=self.ws_a)
        ws_b = afk_toggle.workspace_of(str(self.ws_b))
        self.assertIn(f"- {ws_b}: 2", message)
        self.assertNotIn("gh issue close 2", message)
        left = [json.loads(l)["command"] for l in afk_toggle.queue_path().read_text().splitlines()]
        self.assertEqual(left, ["gh issue close 2", "gh issue close 3"])

    def test_drained_lines_are_kept_in_drained_jsonl(self):
        self.typed("/afk on")
        self.queue(self.ws_a, "gh issue close 1")
        self.queue(self.ws_b, "gh issue close 2")
        self.typed("/afk off", cwd=self.ws_a)
        kept = [json.loads(l)["command"] for l in afk_toggle.drained_path().read_text().splitlines()]
        self.assertEqual(kept, ["gh issue close 1"])


if __name__ == "__main__":
    unittest.main()
