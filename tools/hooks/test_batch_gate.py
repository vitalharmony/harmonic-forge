#!/usr/bin/env python3
"""Tests for batch_gate.py, plus the hook-order-independence proof
required by harmonic-forge#336's amendment (both hooks fed the same
authorized invocation, in both orders, neither produces an ask)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
GATE = HOOK_DIR / "batch_gate.py"
IRREVERSIBLE = HOOK_DIR / "block_irreversible_ops.py"
sys.path.insert(0, str(HOOK_DIR))
import batch_auth as ba  # noqa: E402


class BatchGateUnitTests(unittest.TestCase):
    """batch_gate.py's own logic, exercised directly rather than via
    subprocess -- full end-to-end coverage lives in
    HookOrderIndependenceTests below."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "batch-authorized.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_a_covered_command_emits_allow(self):
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        matched, reason = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertTrue(matched)
        self.assertIn("H395", reason)

    def test_an_uncovered_command_is_silent(self):
        matched, _ = ba.check_and_consume("git clean -fd", state_path=self.state_path)
        self.assertFalse(matched)


class HookOrderIndependenceTests(unittest.TestCase):
    """The exact scenario the amendment names: both PreToolUse hooks
    registered on Bash, fed the same authorized command, in both possible
    execution orders -- neither should leave an `ask` on the table."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "batch-authorized.json"
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        self.command = "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run(self, hook: Path, command: str) -> tuple[int, str]:
        """Run `hook` as a real subprocess (the actual PreToolUse contract),
        pointed at this test's isolated state file via a tiny bootstrap
        script rather than the real STATE_PATH."""
        bootstrap = (
            f"import sys; sys.path.insert(0, {str(HOOK_DIR)!r})\n"
            f"from pathlib import Path\n"
            f"import batch_auth; batch_auth.STATE_PATH = Path({str(self.state_path)!r})\n"
            f"import runpy; runpy.run_path({str(hook)!r}, run_name='__main__')\n"
        )
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout

    def test_gate_then_irreversible_neither_asks(self):
        _rc1, out_gate = self._run(GATE, self.command)
        _rc2, out_irreversible = self._run(IRREVERSIBLE, self.command)
        gate_decision = json.loads(out_gate)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(gate_decision, "allow")
        # block_irreversible_ops.py stays silent (empty stdout) on allow.
        self.assertEqual(out_irreversible.strip(), "")

    def test_irreversible_then_gate_neither_asks(self):
        _rc1, out_irreversible = self._run(IRREVERSIBLE, self.command)
        _rc2, out_gate = self._run(GATE, self.command)
        self.assertEqual(out_irreversible.strip(), "")
        gate_decision = json.loads(out_gate)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(gate_decision, "allow")

    def test_git_clean_still_asks_under_an_unrelated_live_authorization(self):
        """Never a BATCH target -- an unrelated live authorization must not
        leak into it."""
        _rc, out = self._run(IRREVERSIBLE, "git clean -fd")
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "ask")


if __name__ == "__main__":
    unittest.main()
