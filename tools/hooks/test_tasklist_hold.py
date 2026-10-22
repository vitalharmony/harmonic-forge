#!/usr/bin/env python3
"""harmonic-forge#1126 — a lane's task list holds one entry per issue until it closes.

Every test points `tasklist_owed.OWED_DIR`, `tasklist_owed.TASKS_DIR` and
`tasklist_hold.STATE_DIR` at a temp directory and sets `LANE`, so nothing here
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
        for target, name, sub in ((owed, "OWED_DIR", "owed"), (owed, "TASKS_DIR", "tasks"),
                                  (hold, "STATE_DIR", "state")):
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
        self.entry(1, "K65 — closed")
        verdict, reason = hold.pretooluse_decide(self.update(1, "completed"))
        self.assertEqual(verdict, "deny")
        self.assertIn("harmonicarchitect", reason)

    def test_close_requires_closed_subject_before_completion(self):
        self.entry(1, "F1112 — merged 5a1b2c3, deployed · waiting on YOU: close")
        with self.closed(True):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "deny")
            self.entry(1, "F1112 — closed (5a1b2c3)")
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
            self.entry(1, "F1112 — merged")
            payload = self.update(1, "completed", subject="F1112 — closed")
            self.assertEqual(hold.pretooluse_decide(payload)[0], "allow")

    def test_renaming_in_the_completing_call_keeps_the_hold(self):
        """Preclose pass 1: the key is the stored entry's, not the update's."""
        self.entry(1, "H2463 — L2D posted · waiting on Lane 1 review")
        with self.closed(False):
            for subject in ("done", "H1 — closed"):
                payload = self.update(1, "completed", subject=subject)
                self.assertEqual(hold.pretooluse_decide(payload)[0], "deny", subject)

    def test_open_state_is_never_cached(self):
        """Preclose pass 1: a close is seen at once, not after the TTL."""
        open_, closed_ = (subprocess.CompletedProcess([], 0, f"{s}\n", "")
                          for s in ("open", "closed"))
        with mock.patch.object(hold.subprocess, "run", side_effect=[open_, closed_]) as run:
            self.assertFalse(hold.issue_closed("F1112"))
            self.assertTrue(hold.issue_closed("F1112"))
        self.assertEqual(run.call_count, 2)

    def test_closed_state_is_cached(self):
        done = subprocess.CompletedProcess([], 0, "closed\n", "")
        with mock.patch.object(hold.subprocess, "run", return_value=done) as run:
            self.assertTrue(hold.issue_closed("F1112"))
            self.assertTrue(hold.issue_closed("F1112"))
        self.assertEqual(run.call_count, 1)

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
        self.entry(1, "F1112 — merged 5a1b2c3 · waiting on YOU: close")
        owed.record("F1112", "close", session=SESSION)
        self.assertEqual(hold.stop_decide(self.stop())[0], "block")
        self.entry(1, "F1112 — closed (5a1b2c3)")
        with self.closed(True):
            self.assertEqual(hold.pretooluse_decide(self.update(1, "completed"))[0], "allow")
        self.entry(1, "F1112 — closed (5a1b2c3)", status="completed")
        self.assertEqual(hold.stop_decide(self.stop())[0], "allow")
        self.assertFalse((owed.OWED_DIR / SESSION / "F1112.json").exists())

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

    def test_prune_drops_old_sessions(self):
        owed.record("F9", "handoff", session=SESSION)
        old = time.time() - 8 * 86400
        os.utime(owed.OWED_DIR / SESSION, (old, old))
        owed.prune()
        self.assertEqual(owed.markers(SESSION), [])


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

    @staticmethod
    def _roots():
        # Preclose pass 1: the lanes run in HRSE2 too, so its settings are checked
        # as well as this repo's. HRSE2 is the sibling checkout the lanes use;
        # where it is absent (forge CI) only forge's own settings are checked.
        roots = [ROOT]
        hrse2 = Path(os.environ.get("HRSE2_ROOT", Path.home() / "Harmonic_Projects" / "HRSE2"))
        if (hrse2 / ".claude" / "settings.json").is_file():
            roots.append(hrse2)
        return roots

    @staticmethod
    def _missing_wiring(root):
        hooks = json.loads((root / ".claude" / "settings.json").read_text())["hooks"]

        def wired(event, matcher, script):
            return any((matcher is None or e.get("matcher") == matcher)
                       and any(script in h["command"] for h in e["hooks"])
                       for e in hooks.get(event, []))
        wanted = (("PreToolUse", "TaskUpdate", "tasklist_hold.py"),
                  ("Stop", None, "tasklist_hold.py"),
                  ("PostToolUse", "Bash", "record_tasklist_post.py"))
        return [w for w in wanted if not wired(*w)]

    def test_settings_wire_tasklist_hooks(self):
        for root in self._roots():
            with self.subTest(root=str(root)):
                self.assertEqual(self._missing_wiring(root), [])

    def test_an_unwired_hrse2_checkout_is_checked(self):
        """Preclose pass 1 fix C: an HRSE2 checkout without the hooks is caught."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".claude").mkdir()
        (tmp / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
        with mock.patch.dict(os.environ, {"HRSE2_ROOT": str(tmp)}):
            roots = self._roots()
        self.assertIn(tmp, roots)
        self.assertEqual(len(self._missing_wiring(tmp)), 3)

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
