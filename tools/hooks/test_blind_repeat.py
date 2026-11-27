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


class Pass1Fixes(Base):
    def test_a_git_failure_allows_instead_of_keying_an_empty_tree(self):
        self.record_failure("make x")
        real = R._git
        def failing(cwd, *args, **kw):
            return None if args[:1] == ("diff",) else real(cwd, *args, **kw)
        with mock.patch.object(R, "_git", side_effect=failing):
            self.assertIsNone(self.refused("make x"))

    def test_concurrent_failures_are_all_kept(self):
        import threading
        commands = [f"cmd {i}" for i in range(12)]
        threads = [threading.Thread(target=self.record_failure, args=(c,)) for c in commands]
        for t in threads: t.start()
        for t in threads: t.join()
        for c in commands:
            self.assertIsNotNone(self.refused(c), c)

    def test_flaky_count_survives_concurrent_writes(self):
        import threading
        threads = [threading.Thread(target=self.record_failure, args=("mise run check",)) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertIsNotNone(self.refused("mise run check"))


class Pass2Fixes(Base):
    def test_untracked_edit_from_a_subdirectory_clears(self):
        """pass 2 survivor 1: untracked paths are read from the repo top."""
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "new.txt").write_text("a")
        (self.repo / "other.txt").write_text("a")
        self.record_failure("make x", cwd=sub)
        self.assertIsNotNone(self.refused("make x", cwd=sub))
        (sub / "new.txt").write_text("b")
        self.assertIsNone(self.refused("make x", cwd=sub))
        self.record_failure("make x", cwd=sub)
        (self.repo / "other.txt").write_text("b")  # outside the cwd, inside the repo
        self.assertIsNone(self.refused("make x", cwd=sub))

    def test_an_unreadable_untracked_file_gives_no_key(self):
        (self.repo / "locked.txt").write_text("a")
        with mock.patch.object(Path, "open", side_effect=PermissionError):
            self.assertIsNone(R.state_key("make x", str(self.repo)))


class Hygiene(Base):
    def test_other_sessions_young_files_are_kept(self):
        """pass 2 survivor 3: the prune removes only files older than 24 h."""
        young = R.session_file("live-session")
        young.parent.mkdir(parents=True, exist_ok=True)
        young.write_text("")
        recent = time.time() - R.STALE_FILE_SECONDS + 60
        os.utime(young, (recent, recent))
        self.record_failure("make x")
        self.assertTrue(young.exists())

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


class Issue1608Budget(Base):
    """harmonic-forge#1608 P1: the keyed handler must not time out on its allow path."""

    def test_expired_records_never_reach_git(self):  # K1a
        R.record(self.payload("make x"), now=time.time() - R.TTL_SECONDS - 1)
        with mock.patch.object(R, "state_key", side_effect=AssertionError("state_key ran")) as sk:
            self.assertIsNone(self.refused("make x"))
        sk.assert_not_called()

    def test_a_live_record_for_another_command_never_reaches_git(self):  # K1b
        self.record_failure("make x")
        with mock.patch.object(R, "state_key", side_effect=AssertionError("state_key ran")) as sk:
            self.assertIsNone(self.refused("make y"))
        sk.assert_not_called()

    def test_a_record_without_a_cheap_key_allows(self):
        R.session_file(SESSION).parent.mkdir(parents=True, exist_ok=True)
        key = R.state_key("make x", str(self.repo))
        R.session_file(SESSION).write_text(json.dumps({"key": key, "t": time.time()}) + "\n")
        self.assertIsNone(self.refused("make x"))

    def test_a_slow_git_stops_inside_the_budget_and_allows(self):  # K1c
        real_run = subprocess.run

        def slow(argv, *a, **kw):  # every git call takes 2 s, honoring its timeout
            limit = kw.get("timeout")
            if limit is not None and limit < 2:
                time.sleep(max(limit, 0))
                raise subprocess.TimeoutExpired(argv, limit)
            time.sleep(2)
            return real_run(argv, *a, **kw)

        start = time.monotonic()
        with mock.patch.object(R.subprocess, "run", side_effect=slow):
            self.assertIsNone(R.state_key("make x", str(self.repo)))
        self.assertLess(time.monotonic() - start, 4)

    def test_one_slow_large_untracked_file_stops_inside_the_budget_and_allows(self):
        # harmonic-forge#1608 pitch change 2: the deadline is checked per chunk,
        # so one big untracked file cannot run past the budget on its own.
        (self.repo / "big.bin").write_bytes(b"x")

        class Slow(io.RawIOBase):  # 40 chunks at 0.1 s each: 4 s if read whole
            chunks = 40

            def readable(self):
                return True

            def read(self, size=-1):
                if Slow.chunks == 0:
                    return b""
                Slow.chunks -= 1
                time.sleep(0.1)
                return b"x" * 16

        start = time.monotonic()
        with mock.patch.object(R, "_open_for_hash", side_effect=lambda path: Slow()):
            self.assertIsNone(R.state_key("make x", str(self.repo), budget=1.0))
        self.assertLess(time.monotonic() - start, 2.5)

    def test_a_chunked_hash_matches_the_whole_file_hash(self):
        import hashlib
        path = self.repo / "big.bin"
        path.write_bytes(os.urandom(R.HASH_CHUNK_BYTES * 2 + 7))
        self.assertEqual(R._hash_file(path, time.monotonic() + 30),
                         hashlib.sha256(path.read_bytes()).digest())

    def test_a_matching_live_record_still_refuses(self):
        self.record_failure("make x")
        self.assertIsNotNone(self.refused("make x"))


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
