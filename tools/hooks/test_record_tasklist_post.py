#!/usr/bin/env python3
"""harmonic-forge#1126 AC2 — merges, PRs and closes owe a task-list update."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import batch_auth  # noqa: E402
import record_tasklist_post as hook  # noqa: E402
import tasklist_owed as owed  # noqa: E402


def bash(command, stdout=""):
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s1",
            "tool_input": {"command": command},
            "tool_response": {"stdout": stdout, "stderr": ""}}


class Events(unittest.TestCase):
    def test_landed_close_records_close(self):
        with mock.patch.object(batch_auth, "action_landed", return_value=True):
            found = hook.events(bash("gh issue close 1112 -R vitalharmony/harmonic-forge"))
        self.assertEqual(found, [("F1112", "close")])

    def test_bare_close_resolves_the_cwd_repo(self):
        """Preclose pass 1: `gh issue close N` with no repo flag uses gh's own
        repository for the cwd, which the hook now resolves the same way."""
        with mock.patch.object(batch_auth, "action_landed", return_value=True), \
                mock.patch.object(hook, "_cwd_repo", return_value="vitalharmony/harmonic-forge"):
            self.assertEqual(hook.events(bash("gh issue close 1112")), [("F1112", "close")])
        with mock.patch.object(batch_auth, "action_landed", return_value=True), \
                mock.patch.object(hook, "_cwd_repo", return_value=None):
            self.assertEqual(hook.events(bash("gh issue close 1112")), [])

    def test_each_event_is_recorded_as_it_is_found(self):
        """Epoch 2 pass 1: a later failure keeps the earlier marker."""
        seen = []
        with mock.patch.object(batch_auth, "issue_key", side_effect=["F1", RuntimeError("x")]):
            with self.assertRaises(RuntimeError):
                hook.events(bash("gh issue close 1 -R vitalharmony/harmonic-forge; "
                                 "gh issue close 2 -R vitalharmony/harmonic-forge"),
                            on_event=lambda k, kind: seen.append(k))
        self.assertEqual(seen, ["F1"])

    def test_every_close_records_without_reading_whether_it_landed(self):
        """Epoch 3: no landed check and no output reading."""
        with mock.patch.object(batch_auth, "action_landed") as landed:
            found = hook.events(bash("gh issue close 1112 -R vitalharmony/harmonic-forge",
                                     stdout="! Issue #1112 (x) is already closed\n"))
        self.assertEqual(found, [("F1112", "close")])
        landed.assert_not_called()

    def test_landed_merge_records_linked_issue(self):
        with mock.patch.object(batch_auth, "action_landed", return_value=True), \
                mock.patch.object(batch_auth, "derive_issue_key", return_value="F1115"):
            found = hook.events(bash("gh pr merge 1128 -R vitalharmony/harmonic-forge --squash "
                                     "--match-head-commit 0826017"))
        self.assertEqual(found, [("F1115", "merge")])

    def test_pr_create_records_title_issue(self):
        command = ('gh pr create -R vitalharmony/harmonic-forge --head fix/1115-x '
                   '--title "Dry run (harmonic-forge#1115)" --body-file b.md')
        found = hook.events(bash(command, "https://github.com/vitalharmony/harmonic-forge/pull/1128\n"))
        self.assertEqual(found, [("F1115", "pr")])

    def test_other_commands_record_nothing(self):
        self.assertEqual(hook.events(bash("git status && gh issue view 12")), [])


class Main(unittest.TestCase):
    def test_main_records_under_payload_session(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        payload = bash("gh issue close 9 -R vitalharmony/harmonic-forge")
        with mock.patch.object(owed, "OWED_DIR", tmp / "owed"), \
                mock.patch.object(owed, "TASKS_DIR", tmp / "tasks"), \
                mock.patch.dict(os.environ, {"LANE": "1"}), \
                mock.patch.object(batch_auth, "action_landed", return_value=True), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(hook.main(), 0)
            self.assertEqual([(m["key"], m["kind"]) for m in owed.markers("s1")], [("F9", "close")])


if __name__ == "__main__":
    unittest.main()
