"""Tests for block_gate_and_write_same_command (harmonic-forge#1337)."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_gate_and_write_same_command as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_gate_and_write_same_command.py"


class AC1Denied(unittest.TestCase):
    CASES = [
        "mise run check && mise run commit -m x",
        "gh pr checks 1322; gh pr merge 1322",
        "pytest -q | tail -3 && git push",
        "x=$(pytest -q) ; git push",
        'x="$(pytest -q)" ; git push',
        "python3 tools/hooks/preclose_check.py --complete && gh pr merge 1",
        "mise run check-full; mise run restart",
        "gh run watch 5 && git -C /tmp/r commit -m y",
        "python3 -m pytest tests && git push origin main",
        "gh -R o/r pr checks 9 && gh -R o/r pr merge 9",
    ]

    def test_each_is_denied_and_names_both_segments(self):
        for command in self.CASES:
            with self.subTest(command=command):
                reason = hook.decide(command)
                self.assertIsNotNone(reason)
                gate, write = hook.find(command)
                self.assertIn(f"`{gate}`", reason)
                self.assertIn(f"`{write}`", reason)


class AC2Allowed(unittest.TestCase):
    CASES = [
        "mise run check",
        "mise run commit",
        "git commit -m x",
        "mise run check; mise run restart --no-bump --no-git",
        'git commit -m "make pytest pass"',
        'gh issue comment 5 --body "gh pr merge after checks"',
        "git log --grep pytest",
        'echo "x && git push',
        "gh pr checks 5",
        "gh pr merge 5 --squash",
    ]

    def test_each_is_allowed(self):
        for command in self.CASES:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_restart_without_no_git_is_a_write(self):
        self.assertEqual(hook.write_of(["mise", "run", "restart"]), "mise run restart")
        self.assertIsNone(hook.write_of(["mise", "run", "restart", "--no-bump", "--no-git"]))


class PipedGate(unittest.TestCase):
    """Folded in from protocol-failures 2026-11-12: `mise run check | tail -25` hid the
    failing step and the pipe's exit 0 stood in for the check's."""

    def test_piped_gates_are_denied(self):
        for command in ["mise run check 2>&1 | tail -25", "pytest -q | tail -3",
                        "gh pr checks 5 | grep fail", "mise run check-full |& tee log"]:
            with self.subTest(command=command):
                reason = hook.decide(command)
                self.assertIsNotNone(reason)
                self.assertIn("R-0425", reason)

    def test_unpiped_and_non_gate_pipes_are_allowed(self):
        for command in ["mise run check > /tmp/x.log 2>&1", "git log | head",
                        "gh pr view 5 | jq .", "echo pytest | tail -1"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))


class AC3Replay(unittest.TestCase):
    """protocol-failures.jsonl 281 and 287 record the shape, not the literal text:
    281 printed a failing verify result and merged in one command (F1244, PR 1322);
    287 committed the hrse half in the command that printed a failed check (F1319)."""

    def test_281_check_then_merge(self):
        self.assertIsNotNone(hook.decide(
            "gh pr checks 1322 --repo vitalharmony/harmonic-forge; "
            "gh pr merge 1322 --repo vitalharmony/harmonic-forge --squash"))

    def test_287_check_then_commit(self):
        self.assertIsNotNone(hook.decide(
            "mise run check 2>&1 | tail -20; mise run commit --message 'hrse half'"))


class Wire(unittest.TestCase):
    def run_hook(self, payload: dict) -> dict:
        out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_deny_shape(self):
        out = self.run_hook({"tool_name": "Bash",
                             "tool_input": {"command": "mise run check && git push"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allow_and_non_bash(self):
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}), {})
        self.assertEqual(self.run_hook({"tool_name": "Read", "tool_input": {}}), {})

    def test_bad_json_allows(self):
        out = subprocess.run([sys.executable, str(HOOK)], input="not json",
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), {})


if __name__ == "__main__":
    unittest.main()
