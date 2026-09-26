#!/usr/bin/env python3
"""Unit tests for tier_downshift_reminder.py (F769)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import model_tier_gate  # noqa: E402
import tier_downshift_reminder as reminder  # noqa: E402


class DownshiftReminderTests(unittest.TestCase):
    payload = {"transcript_path": "/transcript", "cwd": "/cwd"}

    def run_hook(self, *, lane="2", model="claude-opus-5", branch=False, posted=False):
        with patch.object(reminder.os.path, "isfile", return_value=True), \
             patch.object(reminder, "_is_deep_branch", return_value=branch), \
             patch.object(reminder, "_posted_deep", return_value=posted):
            return reminder.run(self.payload, env={"LANE": lane} if lane else {}, model=model)

    def test_high_tier_without_branch_issue_reminds(self):
        out = self.run_hook()
        self.assertEqual(out["systemMessage"],
                         "This lane is on claude-opus-5 with no deep-tier issue in hand. "
                         "Switch down: /model sonnet")

    def test_deep_branch_is_silent(self):
        self.assertIsNone(self.run_hook(branch=True))

    def test_deep_post_is_silent(self):
        self.assertIsNone(self.run_hook(posted=True))

    def test_sonnet_is_silent(self):
        self.assertIsNone(self.run_hook(model="claude-sonnet-5"))

    def test_lane_unset_is_silent(self):
        self.assertIsNone(self.run_hook(lane=""))

    def test_board_failure_is_silent(self):
        with patch.object(reminder.os.path, "isfile", return_value=True), \
             patch.object(reminder, "_is_deep_branch",
                          side_effect=RuntimeError("board unavailable")):
            self.assertIsNone(reminder.run(self.payload, env={"LANE": "2"},
                                            model="claude-opus-5"))

    def test_post_lookup_failure_is_silent(self):
        with patch.object(reminder.os.path, "isfile", return_value=True), \
             patch.object(reminder, "_is_deep_branch", return_value=False), \
             patch.object(reminder, "_posted_deep", side_effect=RuntimeError("board unavailable")):
            self.assertIsNone(reminder.run(self.payload, env={"LANE": "2"},
                                            model="claude-opus-5"))

    def test_missing_transcript_is_silent(self):
        self.assertIsNone(reminder.run(self.payload, env={"LANE": "2"},
                                        model="claude-opus-5"))

    def test_posted_deep_uses_backstop_posts_and_target_lookup(self):
        calls = []
        with patch.object(reminder.backstop, "scan_turn", return_value=([("post", None, "a")], False)), \
             patch.object(reminder.backstop, "posted_targets",
                          return_value=[("vitalharmony/hrse", 1)]), \
             patch.object(reminder.tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(reminder.tier_model_trigger_check, "lookup_tier",
                          side_effect=lambda repo, issue, boards: (calls.append((repo, issue)) or ("deep", None))):
            self.assertTrue(reminder._posted_deep("/transcript", "/cwd"))
        self.assertEqual(calls, [("vitalharmony/hrse", 1)])

    def test_posted_board_failure_raises_for_quiet_caller(self):
        with patch.object(reminder.backstop, "scan_turn", return_value=([("post", None, "a")], False)), \
             patch.object(reminder.backstop, "posted_targets",
                          return_value=[("vitalharmony/hrse", 1)]), \
             patch.object(reminder.tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(reminder.tier_model_trigger_check, "lookup_tier",
                          return_value=(model_tier_gate.LOOKUP_FAILED, "403")):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")

    def test_truncated_turn_scan_raises_for_quiet_caller(self):
        with patch.object(reminder.backstop, "scan_turn", return_value=([], True)):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")

    def test_post_cap_raises_for_quiet_caller(self):
        calls = [(f"post{i}", None, str(i)) for i in range(reminder._MAX_POST_TIER_READS + 1)]
        with patch.object(reminder.backstop, "scan_turn", return_value=(calls, False)), \
             patch.object(reminder.backstop, "posted_targets",
                          side_effect=lambda command, cwd_repo: [("vitalharmony/hrse", int(command[4:]))]):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")


if __name__ == "__main__":
    unittest.main()
