#!/usr/bin/env python3
"""Tests for the transaction-log SessionStart hook (harmonic-forge#883).

The property that dominates: the hook never blocks session start. Every
failure exits 0 with no output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "transaction_log_context.py"


def run_hook(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    return subprocess.run([sys.executable, str(HOOK), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=30)


class HookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _repo(self, commits: int) -> Path:
        def git(*a):
            subprocess.run(["git", "-C", str(self.dir), *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("config", "commit.gpgsign", "false")
        git("config", "core.hooksPath", "/dev/null")
        git("config", "gc.auto", "0")  # harmonic-forge#871: no detached gc racing cleanup
        git("config", "maintenance.auto", "false")
        for i in range(commits):
            (self.dir / f"f{i}").write_text(str(i))
            git("add", "-A")
            git("commit", "-q", "-m", f"c{i}")
        return self.dir

    def test_emits_session_start_additional_context(self):
        repo = self._repo(3)
        result = run_hook(repo, "--boundary", "recent:30")
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertTrue(out["additionalContext"].startswith("[TRANSACTION LOG] "))
        self.assertIn("## c2\n", out["additionalContext"])

    def test_claude_project_dir_wins_over_cwd(self):
        repo = self._repo(1)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(repo))
        with tempfile.TemporaryDirectory() as elsewhere:
            result = subprocess.run([sys.executable, str(HOOK), "--boundary", "recent:5"],
                                    cwd=elsewhere, env=env, capture_output=True, text=True)
        self.assertIn("## c0", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_outside_a_repo_exits_zero_with_no_output(self):
        result = run_hook(self.dir, "--boundary", "recent:30")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_bad_boundary_exits_zero_with_no_output(self):
        repo = self._repo(1)
        result = run_hook(repo, "--boundary", "version-minor:missing.json")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_missing_boundary_argument_exits_zero_with_no_output(self):
        repo = self._repo(1)
        result = run_hook(repo)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
