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

    def test_receipt_dir_lives_inside_git_dir_not_worktree(self):
        directory = rr.receipt_dir(9006).resolve()
        git_dir = Path(subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()).resolve()
        directory.relative_to(git_dir)  # raises ValueError if not inside


if __name__ == "__main__":
    unittest.main()
