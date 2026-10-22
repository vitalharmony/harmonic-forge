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

    def test_unreadable_task_file_allows(self):
        (self.tasks / "1.json").write_text("{not json")
        self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")

    def test_main_prints_json_deny(self):
        self.entry(1, "H1 — review")
        out = io.StringIO()
        with self.closed(False), mock.patch.object(sys, "stdin", io.StringIO(
                json.dumps(self.update(1, "completed")))), redirect_stdout(out):
            self.assertEqual(hold.main(), 0)
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")


class Stop(Base):
    def setUp(self):
        super().setUp()
        # Stop reads the live state (reforge epoch 2): open unless a test says so.
        live = mock.patch.object(hold, "issue_closed", return_value=False)
        live.start()
        self.addCleanup(live.stop)

    def test_l2d_then_stop_without_entry_update_is_refused(self):
        now = time.time()
        self.entry(1, "H2463 — implementing", mtime=now - 60)
        owed.record("H2463", "completion", session=SESSION, now=now)
        # The stored subject differs from the entry's, so only the time
        # comparison can refuse.
        marker = owed.OWED_DIR / SESSION / "H2463.json"
        marker.write_text(json.dumps({**json.loads(marker.read_text()), "subject": "older"}))
        verdict, reason = hold.stop_decide(self.stop())
        self.assertEqual(verdict, "block")
        self.assertIn("H2463", reason)

    def test_status_only_touch_after_post_is_refused(self):
        now = time.time()
        self.entry(1, "H2463 — implementing", mtime=now - 60)
        owed.record("H2463", "completion", session=SESSION, now=now - 30)
        self.entry(1, "H2463 — implementing", status="in_progress", mtime=now)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")

    def test_rewritten_entry_allows_and_clears(self):
        now = time.time()
        self.entry(1, "H2463 — implementing", mtime=now - 60)
        owed.record("H2463", "completion", session=SESSION, now=now - 30)
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review", mtime=now)
        self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
        self.assertEqual(owed.markers(SESSION), [])

    def test_marker_with_no_entry_is_refused(self):
        owed.record("F9", "handoff", session=SESSION)
        verdict, reason = hold.stop_decide(self.stop())
        self.assertEqual(verdict, "block")
        self.assertIn("no open entry", reason)

    def test_question_turn_is_not_refused(self):
        owed.record("F9", "handoff", session=SESSION)
        transcript = self.tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Should I post the rework now?"}]}}) + "\n")
        self.assertEqual(hold.stop_decide(self.stop(transcript_path=str(transcript)))[0], "allow")

    def test_close_then_closed_subject_then_complete_stop_allows(self):
        now = time.time()
        self.entry(1, "F1112 — merged 5a1b2c3 · waiting on YOU: close", mtime=now - 60)
        owed.record("F1112", "close", session=SESSION, now=now - 30)
        with self.closed(True):
            self.assertEqual(hold.stop_decide(self.stop())[0], "block")
            self.entry(1, "F1112 — closed (5a1b2c3)", mtime=now)
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            self.entry(1, "F1112 — closed (5a1b2c3)", status="completed", mtime=now)
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
        self.assertFalse((owed.OWED_DIR / SESSION / "F1112.json").exists())

    def test_post_on_a_reopened_issue_needs_an_open_entry(self):
        """Reforge epoch 2: an old `— closed` entry is not proof once reopened."""
        now = time.time()
        self.entry(1, "F1112 — closed (abc)", status="completed", mtime=now - 60)
        owed.record("F1112", "handoff", session=SESSION, now=now)
        with self.closed(False):
            verdict, reason = hold.stop_decide(self.stop())
        self.assertEqual(verdict, "block")
        self.assertIn("no open entry", reason)

    def test_second_close_cycle_needs_a_fresh_closed_entry(self):
        """Reforge epoch 2: a `— closed` entry from an earlier close never
        satisfies a later close; the entry must be updated after the marker."""
        now = time.time()
        self.entry(1, "F9 — closed (abc)", status="completed", mtime=now - 600)
        self.entry(2, "F9 — rework posted · waiting on Lane 2 implement", mtime=now - 300)
        owed.record("F9", "close", session=SESSION, now=now)
        with self.closed(True):
            verdict, reason = hold.stop_decide(self.stop())
            self.assertEqual(verdict, "block")
            self.assertIn("F9 — closed", reason)
            self.entry(2, "F9 — closed (def)", mtime=now + 1)
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_close_reopen_post_follows_the_live_state(self):
        """Reforge epoch 2: a post after a close replaces its marker, so a
        reopened issue owes an open-entry update, never a closed subject."""
        now = time.time()
        owed.record("F9", "close", session=SESSION, now=now - 30)
        owed.record("F9", "handoff", session=SESSION, now=now - 20)
        self.entry(1, "F9 — handoff posted · waiting on Lane 2 implement", mtime=now)
        with self.closed(False):
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_the_latest_marker_wins(self):
        owed.record("F9", "close", session=SESSION)
        owed.record("F9", "handoff", session=SESSION)
        marker = json.loads((owed.OWED_DIR / SESSION / "F9.json").read_text())
        self.assertEqual(marker["kind"], "handoff")

    def test_one_unreadable_file_among_valid_ones_allows_stop(self):
        """Reforge epoch 1 (E): a skipped entry would be judged absent. The
        entry was never updated, so only the unreadable-file rule can allow."""
        now = time.time()
        self.entry(1, "F9 — handoff posted · waiting on Lane 2 implement", mtime=now - 60)
        owed.record("F9", "handoff", session=SESSION, now=now)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")
        (self.tasks / "2.json").write_text("{not json")
        (self.tasks / "3.txt").write_text("skipped")
        self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_unreadable_state_judges_the_open_entry_locally(self):
        """Epoch 2 pass 1: K/Y/D (or a failed read) still owes its open entry."""
        now = time.time()
        self.entry(1, "K65 — implementing", mtime=now - 60)
        owed.record("K65", "completion", session=SESSION, now=now - 30)
        with mock.patch.object(hold, "issue_closed", side_effect=RuntimeError("K")):
            self.assertEqual(hold.stop_decide(self.stop())[0], "block")
            self.entry(1, "K65 — L2D posted · waiting on Lane 1 review", mtime=now)
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_spent_read_budget_judges_locally_without_reading(self):
        now = time.time()
        self.entry(1, "F9 — implementing", mtime=now - 60)
        owed.record("F9", "completion", session=SESSION, now=now - 30)
        with mock.patch.object(hold, "STOP_READ_BUDGET", -1), \
                mock.patch.object(hold, "issue_closed") as read:
            self.assertEqual(hold.stop_decide(self.stop())[0], "block")
        read.assert_not_called()

    def test_a_deleted_closed_entry_does_not_wedge_stop(self):
        """Epoch 2 pass 1: AC1 allows deleting a shown closed entry."""
        owed.record("F123", "close", session=SESSION)
        with self.closed(True):
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
        self.assertEqual(owed.markers(SESSION), [])

    def test_a_touched_old_closed_entry_never_shows_a_new_close(self):
        """Epoch 2 pass 1: a status-only touch bumps an old entry's mtime."""
        now = time.time()
        self.entry(1, "F9 — closed (abc)", status="completed", mtime=now - 600)
        self.entry(2, "F9 — rework posted · waiting on Lane 2 implement", mtime=now - 300)
        owed.record("F9", "close", session=SESSION, now=now - 30)
        self.entry(1, "F9 — closed (abc)", status="completed", mtime=now)
        with self.closed(True):
            self.assertEqual(hold.stop_decide(self.stop())[0], "block")

    def test_marker_write_is_atomic(self):
        owed.record("F9", "handoff", session=SESSION)
        before = (owed.OWED_DIR / SESSION / "F9.json").read_text()
        with mock.patch.object(owed.os, "replace", side_effect=OSError("killed")):
            self.assertFalse(owed.record("F9", "close", session=SESSION))
        self.assertEqual((owed.OWED_DIR / SESSION / "F9.json").read_text(), before)
        self.assertEqual([m["kind"] for m in owed.markers(SESSION)], ["handoff"])

    def test_stop_hook_active_allows(self):
        owed.record("F9", "handoff", session=SESSION)
        out = io.StringIO()
        payload = self.stop(stop_hook_active=True)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out):
            hold.main()
        self.assertEqual(out.getvalue(), "")

    def test_unreadable_task_dir_allows_stop(self):
        owed.record("F9", "handoff", session=SESSION)
        shutil.rmtree(self.tasks)
        self.assertEqual(hold.stop_decide(self.stop())[0], "allow")

    def test_no_lane_session_is_exempt(self):
        self.entry(1, "H1 — review")
        owed.record("F9", "handoff", session=SESSION)
        with mock.patch.dict(os.environ, {}, clear=True), self.closed(False):
            self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")


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

    def test_prune_keeps_a_fresh_marker_in_an_old_directory(self):
        """Preclose epoch 1 pass 2: an overwrite leaves the directory's mtime old."""
        owed.record("F9", "handoff", session=SESSION)
        old = time.time() - 8 * 86400
        os.utime(owed.OWED_DIR / SESSION, (old, old))
        owed.record("F9", "handoff", session=SESSION)
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

    def test_recorder_timeout_exceeds_its_network_budget(self):
        """Preclose epoch 1 pass 2: a hook killed at its timeout writes no marker."""
        import record_tasklist_post  # noqa: PLC0415
        hooks = json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"]
        timeouts = [h.get("timeout", 60) for e in hooks.get("PostToolUse", [])
                    for h in e["hooks"] if "record_tasklist_post.py" in h["command"]]
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


if __name__ == "__main__":
    unittest.main()
