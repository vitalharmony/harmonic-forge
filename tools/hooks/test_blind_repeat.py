"""harmonic-forge#1216: the blind-repeat record/block pair."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blind_repeat as R  # noqa: E402
import block_blind_repeat  # noqa: E402
import record_failed_command  # noqa: E402

SESSION = "test-session-1216"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {R.STORE_ENV: str(self.root / "store"),
                                           "GIT_CONFIG_GLOBAL": "/dev/null"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "f").write_text("x")
        _git(self.repo, "add", "f")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")

    def payload(self, command, cwd=None, session=SESSION, **extra):
        return {"session_id": session, "cwd": str(cwd or self.repo),
                "tool_input": {"command": command}, **extra}

    def record_failure(self, command, **kw):
        R.record(self.payload(command, **kw))

    def refused(self, command, **kw):
        return R.refusal(self.payload(command, **kw))


class Blocks(Base):
    def test_identical_repeat_is_refused(self):
        self.record_failure("python3 tools/gh/l1_post.py --issue 1")
        self.assertIn("harmonic-forge#1216", self.refused("python3 tools/gh/l1_post.py --issue 1"))

    def test_lane_shapes_are_refused(self):
        for command in ("mise run l1-post -- --issue 5 --kind handoff",
                        "mise run gate-checkout -- --issue 5",
                        "mise run lane-comment -- --issue 5 --kind spec --file s.md"):
            with self.subTest(command=command):
                self.record_failure(command)
                self.assertIsNotNone(self.refused(command))

    def test_grep_exit_one_repeat_is_refused_inside_the_ttl(self):
        self.record_failure("grep -n nothing f")
        self.assertIsNotNone(self.refused("grep -n nothing f"))

    def test_refusal_skips_the_documented_exit_code_line(self):
        """The documented PostToolUseFailure `error` shape (code.claude.com hooks)."""
        self.record_failure("npm test", error="Exit code 1\nError: Cannot find module 'express'")
        reason = self.refused("npm test")
        self.assertIn("Cannot find module", reason)
        self.assertNotIn("Exit code 1", reason)

    def test_refusal_names_the_recorded_failure(self):
        self.record_failure("make x", error="make: *** No rule to make target 'x'.")
        self.assertIn("No rule to make target", self.refused("make x"))


class Clears(Base):
    def test_a_changed_command_clears(self):
        self.record_failure("make x")
        self.assertIsNone(self.refused("make  x"))

    def test_a_new_head_clears(self):
        self.record_failure("make x")
        (self.repo / "g").write_text("y")
        _git(self.repo, "add", "g")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "d")
        self.assertIsNone(self.refused("make x"))

    def test_editing_an_already_dirty_file_clears(self):
        (self.repo / "f").write_text("dirty 1")
        self.record_failure("make x")
        (self.repo / "f").write_text("dirty 2")
        self.assertIsNone(self.refused("make x"))

    def test_a_new_untracked_file_clears(self):
        self.record_failure("make x")
        (self.repo / "new").write_text("n")
        self.assertIsNone(self.refused("make x"))

    def test_the_ttl_clears_a_remote_state_repeat(self):
        R.record(self.payload("gh pr checks 1"), now=time.time() - R.TTL_SECONDS - 1)
        self.assertIsNone(self.refused("gh pr checks 1"))

    def test_another_session_is_not_refused(self):
        self.record_failure("make x")
        self.assertIsNone(self.refused("make x", session="other"))


class Flaky(Base):
    def test_one_unchanged_retry_then_refused(self):
        self.record_failure("mise run check")
        self.assertIsNone(self.refused("mise run check"))
        self.record_failure("mise run check")
        self.assertIsNotNone(self.refused("mise run check"))

    def test_a_near_miss_gets_no_retry(self):
        self.record_failure("mise run check && echo ok")
        self.assertIsNotNone(self.refused("mise run check && echo ok"))


class FailOpen(Base):
    def test_outside_a_repo_is_neither_recorded_nor_refused(self):
        outside = self.root / "plain"
        outside.mkdir()
        self.record_failure("make x", cwd=outside)
        self.assertFalse(R.session_file(SESSION).exists())
        self.assertIsNone(self.refused("make x", cwd=outside))

    def test_no_session_is_inert(self):
        self.record_failure("make x", session="")
        self.assertFalse((self.root / "store").exists())
        self.assertIsNone(self.refused("make x", session=""))

    def test_a_store_error_allows(self):
        self.record_failure("make x")
        with mock.patch.object(R, "_read", side_effect=OSError):
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(self.payload("make x")))), \
                 mock.patch("sys.stdout", out):
                block_blind_repeat.main()
        self.assertEqual(out.getvalue(), "")

    def test_a_crashing_block_hook_allows(self):
        out = io.StringIO()
        with mock.patch.object(R, "refusal", side_effect=RuntimeError), \
             mock.patch("sys.stdin", io.StringIO("{}")), mock.patch("sys.stdout", out):
            block_blind_repeat.main()
        self.assertEqual(out.getvalue(), "")

    def test_a_git_timeout_allows(self):
        self.record_failure("make x")
        with mock.patch.object(R.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 3)):
            self.assertIsNone(self.refused("make x"))

    def test_a_crashing_recorder_never_raises(self):
        with mock.patch.object(R, "record", side_effect=RuntimeError), \
             mock.patch("sys.stdin", io.StringIO("{}")):
            record_failed_command.main()


class Hygiene(Base):
    def test_other_sessions_stale_files_are_pruned(self):
        stale = R.session_file("old-session")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("")
        past = time.time() - R.STALE_FILE_SECONDS - 10
        os.utime(stale, (past, past))
        self.record_failure("make x")
        self.assertFalse(stale.exists())

    def test_hook_main_emits_deny_json(self):
        self.record_failure("make x")
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(self.payload("make x")))), \
             mock.patch("sys.stdout", out):
            block_blind_repeat.main()
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")


class Wired(unittest.TestCase):
    def test_forge_settings_arm_both_with_a_short_timeout(self):
        settings = json.loads((Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text())
        for event, name in (("PreToolUse", "block_blind_repeat.py"),
                            ("PostToolUseFailure", "record_failed_command.py")):
            entries = [h for e in settings["hooks"][event] if e.get("matcher") == "Bash" for h in e["hooks"]]
            entry = next(h for h in entries if name in h.get("command", ""))
            self.assertLessEqual(entry.get("timeout", 60), 5, name)


if __name__ == "__main__":
    unittest.main()
