#!/usr/bin/env python3
"""Tests for batch_gate.py (harmonic-forge#336, reforged design).

batch_gate.py is now the sole, full-time PreToolUse decision for `gh issue
close`/`gh pr merge` -- these tests exercise it through the real subprocess
stdin/stdout contract, plus the hook-order-independence proof against
block_irreversible_ops.py (which no longer touches these two command
classes at all, so there should be nothing left to reconcile -- these tests
confirm that)."""

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


class BatchGateEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "batch-authorized.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run(self, hook: Path, command: str) -> tuple[int, str]:
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

    def test_a_covered_authorized_command_emits_allow(self):
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        _rc, out = self._run(
            GATE, "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed"
        )
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "allow")

    def test_a_covered_unauthorized_command_emits_ask(self):
        _rc, out = self._run(
            GATE, "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed"
        )
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "ask")

    def test_an_uncovered_command_is_silent(self):
        _rc, out = self._run(GATE, "git clean -fd")
        self.assertEqual(out.strip(), "")

    def test_a_non_bash_tool_is_silent(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}
        proc = subprocess.run(
            [sys.executable, str(GATE)], input=json.dumps(payload),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.stdout.strip(), "")

    def test_malformed_stdin_is_silent(self):
        proc = subprocess.run(
            [sys.executable, str(GATE)], input="not json",
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class HookOrderIndependenceTests(unittest.TestCase):
    """Now that block_irreversible_ops.py no longer decides
    gh issue close/gh pr merge at all, there is nothing for it to
    contribute for these two classes -- these tests confirm it stays
    silent regardless of execution order, and that batch_gate.py alone
    determines the outcome."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "batch-authorized.json"
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        self.command = "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run(self, hook: Path, command: str) -> tuple[int, str]:
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

    def test_gate_then_irreversible(self):
        _rc1, out_gate = self._run(GATE, self.command)
        _rc2, out_irreversible = self._run(IRREVERSIBLE, self.command)
        gate_decision = json.loads(out_gate)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(gate_decision, "allow")
        # block_irreversible_ops.py no longer has an opinion on issue close at all.
        self.assertEqual(out_irreversible.strip(), "")

    def test_irreversible_then_gate(self):
        _rc1, out_irreversible = self._run(IRREVERSIBLE, self.command)
        _rc2, out_gate = self._run(GATE, self.command)
        self.assertEqual(out_irreversible.strip(), "")
        gate_decision = json.loads(out_gate)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(gate_decision, "allow")

    def test_git_clean_still_asks_under_an_unrelated_live_authorization(self):
        """Never a BATCH target -- an unrelated live authorization must not
        leak into it. This is entirely block_irreversible_ops.py's own
        remaining responsibility; batch_gate.py stays silent for it."""
        _rc_g, out_gate = self._run(GATE, "git clean -fd")
        _rc_i, out_irreversible = self._run(IRREVERSIBLE, "git clean -fd")
        self.assertEqual(out_gate.strip(), "")
        decision = json.loads(out_irreversible)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "ask")


if __name__ == "__main__":
    unittest.main()
