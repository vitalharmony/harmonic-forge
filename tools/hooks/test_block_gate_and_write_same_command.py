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


class Pass1Survivors(unittest.TestCase):
    """harmonic-forge#1337 preclose pass 1 (issuecomment-6840160842)."""

    def test_shell_c_and_eval_strings_are_parsed(self):
        for command in ['bash -c "mise run check && git push"',
                        "sh -c 'pytest -q; git commit -m x'",
                        'bash -lc "pytest && git push"',
                        'eval "pytest; git push"']:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_uv_run_prefix_and_mise_alias_and_flags_are_seen_through(self):
        for command in ["uv run pytest && git push", "uv run -- pytest; git push",
                        "mise r check && git push", "mise -C x run check && git push",
                        "mise --cd=x run check; mise r commit"]:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_a_write_before_its_gate_is_allowed(self):
        for command in ["git push -u origin feat/x && gh pr checks 12 --watch",
                        "gh pr merge 5 --squash && gh run watch 9",
                        "git push && gh run view 1"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_read_only_piped_gates_are_allowed(self):
        """The piped-gate fold is removed (outside the AC; R-0381)."""
        for command in ["python3 -m pytest tools/hooks -q | tail -5",
                        "gh run view 123 --log | grep -i error"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.decide(command))

    def test_here_doc_body_is_a_known_residual(self):
        """Operator ruling 2026-11-12, option A: a here-doc fed to a shell is not parsed."""
        self.assertIsNone(hook.decide("bash <<X\npytest\ngit push\nX"))


class Pass2Survivors(unittest.TestCase):
    """Every pass-2 survivor (issuecomment-6840533545) is denied under word-presence
    classification (sticky-wicket pass-2 REFORGE)."""
    DENIED = [
        "mise run preclose-check -- --repo vitalharmony/harmonic-forge --issue 1337 --complete && gh pr merge 1",
        "python3 -I -m pytest -q && git push",
        "python3 -X dev -m pytest && git push",
        "pytest; git --git-dir .git commit -m x",
        "pytest; git --work-tree /tmp/w commit -m x",
        "pytest && exec git push",
        "sudo pytest; git push",
        "2>/dev/null pytest && git push",
        'env X=1 bash -c "pytest && git push"',
        "uv run --with x pytest && git push",
        "mise --cd /tmp/x run check && mise run commit",
    ]

    def test_each_is_denied(self):
        for command in self.DENIED:
            with self.subTest(command=command):
                self.assertIsNotNone(hook.decide(command))

    def test_ordinary_commands_stay_allowed(self):
        for command in ["git log --grep pytest", 'git commit -m "make pytest pass"',
                        "git push && gh pr checks 12 --watch",
                        "pytest -q | tail -5", "gh run view 123 --log | grep -i error",
                        "echo git push"]:
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
