#!/usr/bin/env python3
"""harmonic-forge#1126 — a lane's task list holds one entry per issue until it closes.

Every test points `tasklist_owed.OWED_DIR` and `tasklist_owed.TASKS_DIR` at a
temp directory and sets `LANE`, so nothing here
reads or writes the real stores.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent
ROOT = HOOKS.parents[1]
sys.path.insert(0, str(HOOKS))

import tasklist_hold as hold  # noqa: E402
import tasklist_owed as owed  # noqa: E402

SESSION = "sess-1126"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, name, sub in ((owed, "OWED_DIR", "owed"), (owed, "TASKS_DIR", "tasks")):
            patcher = mock.patch.object(target, name, self.tmp / sub)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"LANE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.tasks = self.tmp / "tasks" / SESSION
        self.tasks.mkdir(parents=True)

    def entry(self, task_id, subject, status="in_progress", mtime=None):
        path = self.tasks / f"{task_id}.json"
        path.write_text(json.dumps({"id": str(task_id), "subject": subject, "status": status,
                                    "blocks": [], "blockedBy": []}))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def update(self, task_id, status, **extra):
        return {"hook_event_name": "PreToolUse", "tool_name": "TaskUpdate", "session_id": SESSION,
                "tool_input": {"taskId": str(task_id), "status": status, **extra}}

    def stop(self, **extra):
        return {"hook_event_name": "Stop", "session_id": SESSION, **extra}

    def closed(self, value):
        return mock.patch.object(hold, "issue_closed", return_value=value)


class PreToolUse(Base):
    def test_completing_only_entry_of_open_issue_is_denied(self):
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
        with self.closed(False):
            verdict, reason = hold.pretooluse_decide(self.update(1, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("H2463", reason)
        self.assertIn(hold.SHAPE, reason)

    def test_deleting_only_entry_of_open_issue_is_denied(self):
        self.entry(1, "F12 — handoff posted · waiting on Lane 2 implement")
        with self.closed(False):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "deleted"))[0], "deny")

    def test_sibling_open_entry_allows(self):
        self.entry(1, "H2463 — read thread")
        self.entry(2, "H2463 — review L2D")
        with self.closed(False):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")

    def test_parallel_completion_of_both_siblings_keeps_one(self):
        """Epoch 3 pass 2 (survivor 1): the second of two parallel completions
        sees the first as pending, not as the survivor."""
        self.entry(1, "H2463 — read thread")
        self.entry(2, "H2463 — review L2D")
        with self.closed(False):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            # Claude Code has not yet written entry 1's completion.
            verdict, reason = hold.pretooluse_decide(self.update(2, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("parallel", reason)

    def test_a_pending_removal_never_expires_until_it_reports(self):
        """Pass 1 (cross-family): a removal held past any TTL still counts."""
        self.entry(1, "H2463 — read thread")
        self.entry(2, "H2463 — review L2D")
        with self.closed(False):
            hold.pretooluse_decide(self.update(1, "completed"))
            old = time.time() - 3600
            os.utime(owed.OWED_DIR / SESSION / ".pending" / "1", (old, old))
            self.assertEqual(hold.pretooluse_decide(self.update(2, "completed"))[0], "deny")
            # A failed update reports back and ends the pending removal.
            payload = {"hook_event_name": "PostToolUseFailure", "tool_name": "TaskUpdate",
                       "session_id": SESSION, "tool_input": {"taskId": "1",
                                                             "status": "completed"}}
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                hold.main()
            self.assertEqual(hold.pretooluse_decide(self.update(2, "completed"))[0], "allow")

    def test_a_non_removing_update_never_ends_a_pending_removal(self):
        """Pass 2 (fail-direction): A1 completes A, A2 edits A's description in
        the same batch and reports first; S must still see A as pending."""
        self.entry(1, "H9 — read thread")
        self.entry(2, "H9 — review")
        with self.closed(False):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            for event in ("PostToolUse", "PostToolUseFailure"):
                payload = {"hook_event_name": event, "tool_name": "TaskUpdate",
                           "session_id": SESSION, "tool_input": {"taskId": "1",
                                                                 "description": "x"}}
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                    hold.main()
            self.assertEqual(hold.pretooluse_decide(self.update(2, "completed"))[0], "deny")

    def test_a_rename_away_ends_its_own_pending_record(self):
        """Post-verdict (codex): a sibling-allowed rename away reports back as a
        subject change, and must not leave the key's removals denied forever."""
        self.entry(1, "H9 — read thread")
        self.entry(2, "H9 — review")
        self.entry(3, "H9 — triage")
        with self.closed(False):
            self.assertEqual(hold.pretooluse_decide(
                self.update(1, None, subject="F1 — other work"))[0], "allow")
            self.assertEqual(owed.pending_removals(SESSION), {"1"})
            self.entry(1, "F1 — other work")
            hold.posttooluse_decide({"hook_event_name": "PostToolUse", "tool_name": "TaskUpdate",
                                     "session_id": SESSION,
                                     "tool_input": {"taskId": "1", "subject": "F1 — other work"}})
            self.assertEqual(owed.pending_removals(SESSION), set())
            self.assertEqual(hold.pretooluse_decide(self.update(2, "completed"))[0], "allow")

    def test_an_unusable_lock_denies(self):
        self.entry(1, "H2463 — read thread")
        self.entry(2, "H2463 — review L2D")
        with self.closed(False), mock.patch.object(owed, "session_lock",
                                                   side_effect=OSError("ro")):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "deny")

    def test_unkeyed_entry_and_status_change_allow(self):
        self.entry(1, "memory fold")
        self.entry(2, "H1 — review")
        self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
        self.assertEqual(hold.pretooluse_decide(self.update(2, "in_progress"))[0], "allow")

    def test_github_read_failure_denies(self):
        self.entry(1, "H2463 — closed")
        failed = subprocess.CompletedProcess([], 1, "", "HTTP 502")
        with mock.patch.object(hold.subprocess, "run", return_value=failed):
            verdict, reason = hold.pretooluse_decide(self.update(1, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("could not be read", reason)

    def test_other_account_key_denies_naming_account(self):
        self.entry(1, "K65 — L2D posted")
        self.entry(2, "K65 — closed", status="completed")
        verdict, reason = hold.pretooluse_decide(self.update(1, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("harmonicarchitect", reason)

    def test_close_requires_closed_subject_before_completion(self):
        self.entry(1, "F1112 — merged 5a1b2c3, deployed · waiting on YOU: close")
        with self.closed(True):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "deny")
            self.entry(1, "F1112 — closed (5a1b2c3)")
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            # Preclose pass 2 (G): renaming to "closed" in the completing call
            # never showed the close, so it is refused; the stored entry rules.
            self.entry(1, "F1112 — merged")
            payload = self.update(1, "completed", subject="F1112 — closed")
            self.assertEqual(hold.pretooluse_decide(payload)[0], "deny")

    def test_renaming_in_the_completing_call_keeps_the_hold(self):
        """Preclose pass 1: the key is the stored entry's, not the update's."""
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
        with self.closed(False):
            for subject in ("done", "H1 — closed"):
                payload = self.update(1, "completed", subject=subject)
                self.assertEqual(hold.pretooluse_decide(payload)[0], "deny", subject)

    def test_every_rename_and_complete_ordering_keeps_an_open_issue(self):
        """Reforge (pass 2: A, G, I): whatever order a session renames and
        completes in, an open issue's only entry cannot leave the list."""
        steps = {"rename-away": {"subject": "done"},
                 "rename-other-key": {"subject": "H1 — closed"},
                 "rename-closed": {"subject": "H2463 — closed"},
                 "complete": {"status": "completed"},
                 "delete": {"status": "deleted"}}
        with self.closed(False):
            for name, change in steps.items():
                for combined in (change, {**change, "status": "completed"}):
                    self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
                    payload = self.update(1, combined.get("status"), **{
                        k: v for k, v in combined.items() if k != "status"})
                    verdict = hold.pretooluse_decide(payload)[0]
                    keeps_key = (combined.get("subject", "H2463").startswith("H2463")
                                 and combined.get("status") is None)
                    with self.subTest(step=name, combined=combined):
                        self.assertEqual(verdict, "allow" if keeps_key else "deny")

    def test_closed_issue_leaves_only_after_the_close_is_shown(self):
        self.entry(1, "H2463 — merged abc · waiting on YOU: close")
        with self.closed(True):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "deny")
            self.assertEqual(hold.pretooluse_decide(
                self.update(1, None, subject="H2463 — closed (abc)"))[0], "allow")
            self.entry(1, "H2463 — closed (abc)")
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")

    def test_a_slow_read_denies_inside_the_hook_timeout(self):
        """Pass 2 (F): the read times out under the hook's 10 s and denies."""
        self.assertLess(hold.GH_TIMEOUT, 10)
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
        with mock.patch.object(hold.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("gh", hold.GH_TIMEOUT)):
            verdict, reason = hold.pretooluse_decide(self.update(1, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("could not be read", reason)

    def test_open_state_is_never_cached(self):
        """Preclose pass 1: a close is seen at once, not after the TTL."""
        open_, closed_ = (subprocess.CompletedProcess([], 0, f"{s}\n", "")
                          for s in ("open", "closed"))
        with mock.patch.object(hold.subprocess, "run", side_effect=[open_, closed_]) as run:
            self.assertFalse(hold.issue_closed("F1112"))
            self.assertTrue(hold.issue_closed("F1112"))
        self.assertEqual(run.call_count, 2)

    def test_closed_state_is_never_cached(self):
        """Reforge epoch 1: a reopen made anywhere is seen on the next read."""
        closed_, open_ = (subprocess.CompletedProcess([], 0, f"{s}\n", "")
                          for s in ("closed", "open"))
        self.entry(1, "F1112 — closed (abc)")
        with mock.patch.object(hold.subprocess, "run", side_effect=[closed_, open_]) as run:
            self.assertTrue(hold.issue_closed("F1112"))
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "deny")
        self.assertEqual(run.call_count, 2)

    def test_other_account_closed_subject_completes_without_a_read(self):
        """Reforge epoch 1 (C): a K/Y/D `— closed` entry leaves on the lane's word."""
        self.entry(1, "K12 — closed")
        with mock.patch.object(hold.subprocess, "run") as run:
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
        run.assert_not_called()
        self.entry(1, "K12 — L2D posted")
        self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "deny")

    def test_unreadable_task_file_denies(self):
        """Epoch 3 (AC1): an unreadable task file denies; it never lifts the hold."""
        self.entry(1, "H2463 — closed (abc)")
        with self.closed(True):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            (self.tasks / "2.json").write_text("{not json")
            verdict, reason = hold.pretooluse_decide(self.update(1, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("could not be read", reason)

    def test_an_empty_proposed_subject_is_applied(self):
        """Epoch 3 pass 1: '' removes the key, so it is a rename away."""
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
        with self.closed(False):
            self.assertEqual(hold.pretooluse_decide(self.update(1, None, subject=""))[0], "deny")

    def test_an_unexpected_error_denies(self):
        """Pass 1 (fail-direction): any exception on the PreToolUse path denies."""
        self.entry(1, "H1 — review")
        out = io.StringIO()
        with mock.patch.object(hold, "issue_closed", side_effect=ImportError("batch_auth")), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                    self.update(1, "completed")))), redirect_stdout(out):
            hold.main()
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"],
                         "deny")

    def test_the_lock_is_the_task_directory_and_survives_prune(self):
        """Pass 1 (fail-direction): no lock file of ours for prune to unlink."""
        owed.record("F9", "handoff", session=SESSION)
        old = time.time() - 8 * 86400
        os.utime(owed.OWED_DIR / SESSION / "F9.json", (old, old))
        with owed.session_lock(SESSION):
            owed.prune()
            self.assertFalse((owed.OWED_DIR / SESSION / ".lock").exists())
        self.assertTrue(self.tasks.is_dir())

    def test_main_prints_json_deny(self):
        self.entry(1, "H1 — review")
        out = io.StringIO()
        with self.closed(False), mock.patch.object(sys, "stdin", io.StringIO(
                json.dumps(self.update(1, "completed")))), redirect_stdout(out):
            self.assertEqual(hold.main(), 0)
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")


class Stop(Base):
    """Reforge epoch 4: a marker is cleared only by PostToolUse on a
    TaskCreate/TaskUpdate that gives an entry carrying the key its subject;
    Stop compares nothing."""

    def post(self, tool, **tool_input):
        return {"hook_event_name": "PostToolUse", "tool_name": tool, "session_id": SESSION,
                "tool_input": tool_input}

    def test_l2d_then_stop_without_entry_update_is_refused(self):
        self.entry(1, "H2463 — implementing")
        owed.record("H2463", "completion", session=SESSION)
        verdict, reason = hold.stop_decide(self.stop())
        self.assertEqual(verdict, "block")
        self.assertIn("H2463", reason)
        self.assertIn(hold.SHAPE, reason)

    def test_status_only_update_does_not_settle(self):
        # The entry is open and carries the key, so only the missing subject
        # can keep the marker (kill check at b323003: no entry was vacuous).
        owed.record("H2463", "completion", session=SESSION, now=time.time() - 5)
        self.entry(1, "H2463 — implementing")
        self.assertIsNone(hold.posttooluse_decide(self.post("TaskUpdate", taskId="1",
                                                            status="in_progress")))
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")

    def test_rewritten_subject_settles_and_clears(self):
        owed.record("H2463", "completion", session=SESSION, now=time.time() - 5)
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
        self.assertEqual(hold.posttooluse_decide(self.post(
            "TaskUpdate", taskId="1", subject="H2463 — L2D posted · waiting on Lane 1 review")),
            "H2463")
        self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
        self.assertEqual(owed.markers(SESSION), [])

    def test_a_created_entry_settles_a_key_that_had_none(self):
        """Pass 2 survivors 2 and 5: no history guess; creating the entry settles it."""
        owed.record("H2500", "completion", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")
        self.entry(1, "H2500 — L2D posted", mtime=time.time() + 5)
        hold.posttooluse_decide(self.post("TaskCreate", subject="H2500 — L2D posted"))
        self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_a_reopened_issue_with_only_a_completed_entry_owes_one(self):
        """Pass 2 survivor 2: a completed entry from an earlier close never settles."""
        self.entry(1, "F900 — closed (abc)", status="completed")
        owed.record("F900", "handoff", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")

    def test_another_keys_subject_does_not_settle(self):
        owed.record("F9", "handoff", session=SESSION)
        hold.posttooluse_decide(self.post("TaskUpdate", taskId="1", subject="F90 — review"))
        hold.posttooluse_decide(self.post("TaskCreate", subject="memory fold F9"))
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["F9"])

    def test_a_later_post_needs_a_later_update(self):
        self.entry(1, "F9 — handoff")
        owed.record("F9", "handoff", session=SESSION)
        hold.posttooluse_decide(self.post("TaskUpdate", taskId="1", subject="F9 — handoff"))
        owed.record("F9", "close", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")

    def test_close_then_closed_subject_then_complete_stop_allows(self):
        self.entry(1, "F1112 — merged 5a1b2c3 · waiting on YOU: close")
        owed.record("F1112", "close", session=SESSION)
        with self.closed(True):
            self.assertEqual(hold.stop_decide(self.stop())[0], "block")
            self.entry(1, "F1112 — closed (5a1b2c3)")
            hold.posttooluse_decide(self.post("TaskUpdate", taskId="1",
                                              subject="F1112 — closed (5a1b2c3)"))
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_question_turn_is_not_refused(self):
        owed.record("F9", "handoff", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")  # owed without the guard
        transcript = self.tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Should I post the rework now?"}]}}) + "\n")
        self.assertEqual(hold.stop_decide(self.stop(transcript_path=str(transcript)))[0], "allow")

    def test_the_latest_marker_wins(self):
        owed.record("F9", "close", session=SESSION)
        owed.record("F9", "handoff", session=SESSION)
        marker = json.loads((owed.OWED_DIR / SESSION / "F9.json").read_text())
        self.assertEqual(marker["kind"], "handoff")
        self.assertNotIn("subject", marker)

    def test_marker_write_is_atomic(self):
        owed.record("F9", "handoff", session=SESSION)
        before = (owed.OWED_DIR / SESSION / "F9.json").read_text()
        with mock.patch.object(owed.os, "replace", side_effect=OSError("killed")):
            self.assertFalse(owed.record("F9", "close", session=SESSION))
        self.assertEqual((owed.OWED_DIR / SESSION / "F9.json").read_text(), before)

    def test_stop_hook_active_allows(self):
        owed.record("F9", "handoff", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")  # owed without the guard
        out = io.StringIO()
        payload = self.stop(stop_hook_active=True)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out):
            hold.main()
        self.assertEqual(out.getvalue(), "")

    def test_removing_an_entry_never_settles_its_key(self):
        """Pass 1 (correctness, fail-direction, second-run): a rename that
        completes or deletes in the same call leaves no rewritten entry."""
        # The marker predates the entry writes, so only the status check can
        # keep it (kill check at 9a8eb1f).
        owed.record("H5", "completion", session=SESSION, now=time.time() - 5)
        self.entry(1, "H5 — implementing")
        self.entry(2, "H5 — rebase")
        self.entry(3, "run lint")
        for task_id, status in (("1", "completed"), ("3", "deleted")):
            hold.posttooluse_decide(self.post("TaskUpdate", taskId=task_id, status=status,
                                              subject="H5 — L2D posted"))
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["H5"])

    def test_renaming_a_left_entry_never_settles_its_key(self):
        """Pass 1 (second-run): the renamed entry must still be open."""
        owed.record("H5", "completion", session=SESSION, now=time.time() - 5)
        self.entry(1, "H5 — L2D posted", status="completed")
        hold.posttooluse_decide(self.post("TaskUpdate", taskId="1", subject="H5 — L2D posted"))
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["H5"])

    def test_a_post_recorded_after_the_entry_write_keeps_its_marker(self):
        """Pass 2 (codex, sticky PATCH): a late clear never erases a newer post."""
        now = time.time()
        self.entry(1, "H5 — L2D posted", mtime=now - 10)
        owed.record("H5", "rework", session=SESSION, now=now)
        hold.posttooluse_decide(self.post("TaskUpdate", taskId="1", subject="H5 — L2D posted"))
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["H5"])

    def test_main_routes_posttooluse_to_the_clear(self):
        owed.record("F9", "handoff", session=SESSION, now=time.time() - 5)
        self.entry(1, "F9 — handoff posted")
        payload = self.post("TaskUpdate", taskId="1", subject="F9 — handoff posted")
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(io.StringIO()):
            hold.main()
        self.assertEqual(owed.markers(SESSION), [])

    def test_no_lane_session_is_exempt(self):
        self.entry(1, "H1 — review")
        owed.record("F9", "handoff", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")  # owed without the guard
        with mock.patch.dict(os.environ, {}, clear=True), self.closed(False):
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            self.assertIsNone(hold.posttooluse_decide(self.post("TaskCreate", subject="F9 — x")))


class Store(Base):
    def test_record_without_session_writes_nothing(self):
        self.assertFalse(owed.record("F9", "handoff", session=""))
        self.assertFalse((self.tmp / "owed").exists())

    def test_record_post_resolves_key_and_never_raises(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            owed.record_post("vitalharmony/harmonic-forge", 1126, "handoff")
            with mock.patch.object(owed, "record", side_effect=RuntimeError("boom")):
                owed.record_post("vitalharmony/harmonic-forge", 1126, "handoff")
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["F1126"])

    def test_prune_drops_old_markers(self):
        owed.record("F9", "handoff", session=SESSION)
        old = time.time() - 8 * 86400
        os.utime(owed.OWED_DIR / SESSION / "F9.json", (old, old))
        owed.prune()
        self.assertEqual(owed.markers(SESSION), [])
        self.assertFalse((owed.OWED_DIR / SESSION).exists())

    def test_prune_never_drops_a_pending_removal(self):
        """Pass 2 (codex, sticky PATCH): an old pending record still protects."""
        owed.mark_pending(SESSION, "1")
        old = time.time() - 30 * 86400
        os.utime(owed.OWED_DIR / SESSION / ".pending" / "1", (old, old))
        owed.prune()
        self.assertEqual(owed.pending_removals(SESSION), {"1"})

    def test_prune_keeps_a_fresh_marker_in_an_old_directory(self):
        """Preclose epoch 1 pass 2: an overwrite leaves the directory's mtime old."""
        owed.record("F9", "handoff", session=SESSION)
        old = time.time() - 8 * 86400
        os.utime(owed.OWED_DIR / SESSION, (old, old))
        owed.prune()
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["F9"])


def _calls_record_post(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "record_post" for node in ast.walk(tree))


class Wiring(unittest.TestCase):
    def test_l2_post_records_marker(self):
        self.assertTrue(_calls_record_post(ROOT / "tools" / "gh" / "l2_post.py"))

    def test_l1_post_and_post_comment_record_markers(self):
        for name in ("l1_post.py", "post_comment.py"):
            self.assertTrue(_calls_record_post(ROOT / "tools" / "gh" / name), name)

    def test_settings_wire_tasklist_hooks(self):
        # Preclose pass 2 (H): forge checks its own settings only. HRSE2's
        # wiring is checked by HRSE2's own test (scripts/test_tasklist_hooks_wired.py),
        # which HRSE2's CI runs; a forge test that skipped HRSE2 where it is
        # absent covered nothing.
        hooks = json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"]

        def wired(event, matcher, script):
            return any((matcher is None or e.get("matcher") == matcher)
                       and any(script in h["command"] for h in e["hooks"])
                       for e in hooks.get(event, []))
        self.assertTrue(wired("PreToolUse", "TaskUpdate", "tasklist_hold.py"))
        self.assertTrue(wired("Stop", None, "tasklist_hold.py"))
        self.assertTrue(wired("PostToolUse", "Bash", "record_tasklist_post.py"))
        # Epoch 4: the marker clear, and the recorder on a failed Bash call
        # (AC0 probe: a nonzero Bash call routes to PostToolUseFailure).
        self.assertTrue(wired("PostToolUse", "TaskCreate|TaskUpdate", "tasklist_hold.py"))
        self.assertTrue(wired("PostToolUseFailure", "Bash", "record_tasklist_post.py"))
        self.assertTrue(wired("PostToolUseFailure", "TaskUpdate", "tasklist_hold.py"))

    def test_recorder_timeout_exceeds_its_network_budget(self):
        """Preclose epoch 1 pass 2: a hook killed at its timeout writes no marker."""
        import record_tasklist_post  # noqa: PLC0415
        hooks = json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"]
        timeouts = [h.get("timeout", 60)
                    for event in ("PostToolUse", "PostToolUseFailure")
                    for e in hooks.get(event, []) for h in e["hooks"] if "record_tasklist_post.py" in h["command"]]
        self.assertTrue(timeouts)
        self.assertTrue(all(t > record_tasklist_post.NETWORK_BUDGET for t in timeouts), timeouts)

    def test_entry_shape_lives_only_in_hook(self):
        files = [ROOT / "3-lane-protocol.md", *sorted((ROOT / "rules").glob("*.md"))]
        hits = [f"{f.name}:{n}" for f in files
                for n, line in enumerate(f.read_text().splitlines(), 1) if "· waiting on" in line]
        self.assertEqual(hits, [])
        self.assertIn(hold.SHAPE, hold.__doc__)
        r0413 = re.search(r"<!-- R-0413 -->\n(.*?)\n<!-- /R-0413 -->",
                          (ROOT / "3-lane-protocol.md").read_text(), re.S).group(1)
        self.assertIn("tools/hooks/tasklist_hold.py", r0413)


class OtherLanePostTests(Base):
    """harmonic-forge#1221: another lane's post on a held issue owes the entry
    an update, recorded by the belt's comment watch."""

    POSTED = "2026-10-28T12:00:00Z"

    def _posted(self):
        return owed._epoch(self.POSTED)

    def stale(self, **kw):
        return owed.record_if_stale("vitalharmony/hrse", 2384, "l1 — ready-for-l3",
                                    kw.pop("created_at", self.POSTED), session=SESSION, **kw)

    def test_an_entry_older_than_the_post_is_marked(self):
        """AC1."""
        self.entry(1, "H2384 — L2D ca2ca1733 · waiting on Lane 1 review", "pending",
                   mtime=self._posted() - 60)
        self.assertTrue(self.stale())
        self.assertEqual([m["key"] for m in owed.markers(SESSION)], ["H2384"])
        verdict, _reason = hold.stop_decide({"session_id": SESSION})
        self.assertEqual(verdict, "block")

    def test_an_entry_written_after_the_post_is_not_marked(self):
        """AC2."""
        self.entry(1, "H2384 — ready-for-l3 posted · waiting on Lane 3 spec", "pending",
                   mtime=self._posted() + 60)
        self.assertFalse(self.stale())
        self.assertEqual(owed.markers(SESSION), [])

    def test_no_open_entry_records_nothing(self):
        self.entry(1, "H2384 — closed", "completed", mtime=self._posted() - 60)
        self.assertFalse(self.stale())
        self.assertEqual(owed.markers(SESSION), [])

    def test_a_k_key_records_nothing(self):
        """AC4."""
        self.entry(1, "K65 — handoff posted · waiting on Lane 2", "pending",
                   mtime=self._posted() - 60)
        with mock.patch.object(owed, "key_for", return_value="K65"):
            self.assertFalse(self.stale())
        self.assertEqual(owed.markers(SESSION), [])

    def test_an_unreadable_task_list_never_raises(self):
        """AC6."""
        with mock.patch.object(owed, "read_entries", side_effect=OSError("denied")):
            self.assertFalse(self.stale())
        self.assertEqual(owed.markers(SESSION), [])

    def test_an_unparseable_created_at_never_raises(self):
        """AC6."""
        self.entry(1, "H2384 — L2D · waiting on Lane 1 review", "pending",
                   mtime=self._posted() - 60)
        self.assertFalse(self.stale(created_at="not-a-date"))
        self.assertEqual(owed.markers(SESSION), [])

    def test_a_resolver_that_cannot_load_is_named_on_stderr(self):
        """AC6 (pass-1 survivor 2): a batch_auth import failure is logged, not
        mistaken for an unknown repo."""
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"batch_auth": None}), \
                mock.patch("sys.stderr", err):
            self.assertIsNone(owed.key_for("vitalharmony/hrse", 2384))
        self.assertIn("key resolver unavailable", err.getvalue())

    def test_an_unknown_repo_stays_silent(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertIsNone(owed.key_for("nobody/nothing", 1))
        self.assertEqual(err.getvalue(), "")

    def test_a_raising_resolver_is_named_on_stderr(self):
        """AC6 (pass-2 survivor 2)."""
        import batch_auth  # noqa: PLC0415
        err = io.StringIO()
        with mock.patch.object(batch_auth, "issue_key", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stderr", err):
            self.assertIsNone(owed.key_for("vitalharmony/hrse", 2384))
        self.assertIn("key resolve failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
