"""Focused contract tests for the sanctioned commit/push lifecycle."""

import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("git_commit.py")
SPEC = importlib.util.spec_from_file_location("git_commit", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ExecuteGitTests(unittest.TestCase):
    def test_failure_surfaces_git_stderr_before_raising(self):
        failure = subprocess.CompletedProcess(
            ["git", "push"], 128, stdout="", stderr="fatal: no upstream branch\n"
        )
        stderr = io.StringIO()
        with patch.object(MODULE.subprocess, "run", return_value=failure), patch.object(
            MODULE.sys, "stderr", stderr
        ), self.assertRaisesRegex(subprocess.CalledProcessError, "128"):
            MODULE.execute_git(["git", "push"])
        self.assertEqual(stderr.getvalue(), "fatal: no upstream branch\n")

    def test_each_feature_push_uses_head_and_sets_upstream(self):
        calls: list[list[str]] = []

        def fake_execute(cmd, check=True):
            calls.append(cmd)
            return "" if cmd[:3] == ["git", "status", "--porcelain"] else "feature"

        with patch.object(MODULE, "execute_git", side_effect=fake_execute), patch.object(
            MODULE.sys, "argv", ["git_commit.py", "--push"]
        ):
            MODULE.main()
            MODULE.main()
        pushes = [call for call in calls if call[:2] == ["git", "push"]]
        self.assertEqual(pushes, [["git", "push", "-u", "origin", "HEAD"]] * 2)

    def test_main_still_enters_main_only_log_rotation_path(self):
        calls: list[list[str]] = []

        def fake_execute(cmd, check=True):
            calls.append(cmd)
            if cmd[:3] == ["git", "status", "--porcelain"]:
                return ""
            if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return "main"
            return ""

        clear = subprocess.CompletedProcess(["clear"], 0, stdout="not-cleared\n", stderr="")
        with patch.object(MODULE, "execute_git", side_effect=fake_execute), patch.object(
            MODULE.subprocess, "run", return_value=clear
        ) as mocked_run, patch.object(MODULE.sys, "argv", ["git_commit.py", "--push"]):
            MODULE.main()
        self.assertIn(["git", "push", "-u", "origin", "HEAD"], calls)
        mocked_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
