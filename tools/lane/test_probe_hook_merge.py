#!/usr/bin/env python3
"""Tests for `probe_hook_merge.py`, the hook-merge re-measurement tool
(harmonic-forge#1307). Moved out of `test_lane_hookset.py` unchanged
(harmonic-forge#1526) so that file stays within the line cap."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


class ProbeFixture(unittest.TestCase):
    """The re-measurement tool stays runnable; without --real-run it launches
    nothing, which is all a test may do."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_every_variant_writes_a_fixture_and_launches_nothing(self):
        proc = subprocess.run([sys.executable, str(HERE / "probe_hook_merge.py"),
                               "--dir", str(self.tmp)], capture_output=True, text=True,
                              env={**os.environ, "PATH": "/nonexistent"}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("== "), 4)
        self.assertTrue((self.tmp / "proj" / ".claude" / "settings.json").is_file())

    def test_variant_and_env_select_without_launching(self):
        """The refused-env sweep's measurement form, launch-free."""
        base = [sys.executable, str(HERE / "probe_hook_merge.py"), "--dir", str(self.tmp)]
        env = {**os.environ, "PATH": "/nonexistent"}
        proc = subprocess.run([*base, "--variant", "merge", "--env", "CLAUDE_CODE_X=1"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("== "), 1)
        self.assertIn("env: CLAUDE_CODE_X=1", proc.stdout)
        proc = subprocess.run([*base, "--env", "NOEQUALS"], capture_output=True, text=True,
                              env=env, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("NAME=VALUE", proc.stderr)

    def test_other_hooks_are_the_cli_count_less_the_stub_count(self):
        import probe_hook_merge as probe
        log = self.tmp / "log.txt"
        log.write_text("A event=PreToolUse\nB event=PreToolUse\n", encoding="utf-8")
        stream = "\n".join(json.dumps({"type": "system", "subtype": "hook_started",
                                       "hook_event": ev}) for ev in
                           ["PreToolUse"] * 5 + ["Stop"]) + "\nnot json\n"
        self.assertEqual(probe.other_hooks(stream, log), [
            "      3 OTHER PreToolUse (cli reported 5, stub 2)",
            "      1 OTHER Stop (cli reported 1, stub 0)"])


if __name__ == "__main__":
    unittest.main()
