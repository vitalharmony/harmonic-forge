"""harmonic-forge#1338: afk_defer turns batch_auth's `ask` into deny plus queue."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import afk_defer
import afk_toggle


def _bash(command, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
            "session_id": "s-1", "cwd": str(cwd)}


class DeferCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "LANE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def on(self):
        afk_toggle.toggle({"prompt": "/afk on", "cwd": str(self.home)}, "cli")

    def lines(self):
        path = afk_toggle.queue_path()
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_close_denied_and_queued_when_on(self):
        self.on()
        # The handoff's case set names `gh -R o/r issue close 5`; batch_auth.decide()
        # at 8372c04 returns None for that form (`-R` before the subcommand), so it
        # is not covered here -- reported to Lane 1, not patched in batch_auth.
        cases = ["gh issue close 5", "gh issue close 5 -R o/r", "GH_REPO=x gh issue close 5",
                 "gh api -X PATCH repos/o/r/issues/5 -f state=closed"]
        for command in cases:
            self.assertEqual(afk_defer.defer(_bash(command, self.home)), afk_defer.DENY_MESSAGE, command)
        queued = self.lines()
        self.assertEqual([e["command"] for e in queued], cases)
        for entry in queued:
            self.assertEqual(entry["session"], "s-1")
            self.assertEqual(entry["lane"], "1")
            self.assertEqual(entry["cwd"], str(self.home))
            self.assertTrue(entry["ts"] and entry["workspace"] and entry["reason_head"])

    def test_inert_when_off(self):
        self.assertIsNone(afk_defer.defer(_bash("gh issue close 5", self.home)))
        self.assertEqual(self.lines(), [])

    def test_allowed_gh_command_passes(self):
        self.on()
        for command in ("gh issue view 5", "ls -la", "git status"):
            self.assertIsNone(afk_defer.defer(_bash(command, self.home)), command)
        self.assertEqual(self.lines(), [])

    def test_off_between_check_and_append_allows(self):
        self.on()
        real = afk_toggle.append_queue

        def off_first(entry):
            afk_toggle.toggle({"prompt": "/afk off", "cwd": str(self.home)}, "cli")
            return real(entry)

        with mock.patch.object(afk_toggle, "append_queue", off_first):
            self.assertIsNone(afk_defer.defer(_bash("gh issue close 5", self.home)))
        self.assertEqual(self.lines(), [])

    def test_parse_failure_ask_stays_with_batch_auth(self):
        self.on()
        self.assertIsNone(afk_defer.defer(_bash("echo $'it\\'s ok'", self.home)))
        self.assertEqual(afk_defer.defer(_bash("gh issue close 5 -R o/r", self.home)),
                         afk_defer.DENY_MESSAGE)
        self.assertEqual([e["command"] for e in self.lines()], ["gh issue close 5 -R o/r"])


if __name__ == "__main__":
    unittest.main()
