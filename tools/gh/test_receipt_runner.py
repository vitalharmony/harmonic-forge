#!/usr/bin/env python3
"""Unit tests for receipt_runner.py (harmonic-forge#371).

Run: python3 tools/gh/test_receipt_runner.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import receipt_runner as rr


class TestReceiptRunner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        self._cwd = Path.cwd()
        import os
        os.chdir(self._tmp.name)

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_successful_command_writes_receipt_no_lock(self):
        exit_code = rr.run_command(9001, ["echo", "hello"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(rr.is_locked(9001))
        receipts = list(rr.receipt_dir(9001).glob("*-command.json"))
        self.assertEqual(len(receipts), 1)
        body = json.loads(receipts[0].read_text())
        self.assertEqual(body["exit_code"], 0)
        self.assertEqual(body["argv"], ["echo", "hello"])
        self.assertEqual(body["stdout_sha256"], rr._digest(b"hello\n"))

    def test_failing_command_writes_receipt_and_locks(self):
        exit_code = rr.run_command(9002, ["false"])
        self.assertEqual(exit_code, 1)
        self.assertTrue(rr.is_locked(9002))
        lock = json.loads(rr.lock_path(9002).read_text())
        self.assertEqual(lock["exit_code"], 1)

    def test_clear_lock_removes_marker(self):
        rr.run_command(9003, ["false"])
        self.assertTrue(rr.is_locked(9003))
        rr.clear_lock(9003)
        self.assertFalse(rr.is_locked(9003))

    def test_receipts_are_issue_scoped(self):
        rr.run_command(9004, ["false"])
        self.assertTrue(rr.is_locked(9004))
        self.assertFalse(rr.is_locked(9005))

    @staticmethod
    def _print_exact(text: str) -> list[str]:
        """A command that writes exactly `text` to stdout, byte for byte --
        `python3 -c` with a `repr()`-embedded literal, not a shell printf
        (whose own backslash handling would mangle the very escape bytes
        this test needs to be real)."""
        return ["python3", "-c", f"import sys; sys.stdout.write({text!r})"]

    def test_ansi_color_stripped_from_preview_real_vite_fragment(self):
        """harmonic-forge#571 AC1/AC2 -- a real captured fragment (the exact
        shape that broke l2_post.py's self-check live), not a synthetic
        string that happens to dodge the case."""
        raw = ("\x1b[33m[INEFFECTIVE_DYNAMIC_IMPORT]\x1b[39m /src/app.ts is "
               "dynamically imported but also statically imported\n")
        exit_code = rr.run_command(9007, self._print_exact(raw))
        self.assertEqual(exit_code, 0)
        receipts = list(rr.receipt_dir(9007).glob("*-command.json"))
        body = json.loads(receipts[0].read_text())
        self.assertNotIn("\x1b", body["stdout_preview"])
        self.assertIn("[INEFFECTIVE_DYNAMIC_IMPORT]", body["stdout_preview"])

    def test_digest_is_computed_over_raw_unstripped_output(self):
        """The digest proves exactly what ran, byte for byte -- stripping it
        would make the digest prove something other than the real output."""
        raw = "\x1b[33mcolored\x1b[39m"
        exit_code = rr.run_command(9008, self._print_exact(raw))
        self.assertEqual(exit_code, 0)
        receipts = list(rr.receipt_dir(9008).glob("*-command.json"))
        body = json.loads(receipts[0].read_text())
        self.assertEqual(body["stdout_sha256"], rr._digest(raw.encode()))

    def test_strip_ansi_leaves_plain_text_untouched(self):
        self.assertEqual(rr.strip_ansi("plain text, no escapes"), "plain text, no escapes")

    def test_strip_ansi_removes_sgr_reset_too(self):
        self.assertEqual(rr.strip_ansi("\x1b[0m\x1b[1;32mgreen\x1b[0m"), "green")

    def test_strip_ansi_removes_osc_hyperlink_bel_terminated(self):
        """harmonic-forge#571 preclose finding: OSC (colour is not the only
        VT100 sequence a terminal-aware CLI emits)."""
        text = "\x1b]8;;https://example.com\x07click here\x1b]8;;\x07"
        self.assertEqual(rr.strip_ansi(text), "click here")

    def test_strip_ansi_removes_osc_title_st_terminated(self):
        text = "\x1b]0;My Title\x1b\\done"
        self.assertEqual(rr.strip_ansi(text), "done")

    def test_strip_ansi_removes_charset_select(self):
        self.assertEqual(rr.strip_ansi("\x1b(Bplain"), "plain")

    def test_strip_ansi_removes_cursor_save_restore(self):
        self.assertEqual(rr.strip_ansi("a\x1b7b\x1b8c"), "abc")

    def test_receipt_dir_lives_inside_git_dir_not_worktree(self):
        directory = rr.receipt_dir(9006).resolve()
        git_dir = Path(subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()).resolve()
        directory.relative_to(git_dir)  # raises ValueError if not inside


if __name__ == "__main__":
    unittest.main()
