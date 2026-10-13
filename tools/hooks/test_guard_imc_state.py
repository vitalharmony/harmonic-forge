#!/usr/bin/env python3
"""AC4 (harmonic-forge#895): the IMC state guard denies any tool call naming
the state directory, and allows exactly the `imc_waiver.py record` command."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guard_imc_state as g  # noqa: E402


def bash(command: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


class GuardTests(unittest.TestCase):
    def assertDenied(self, payload: dict) -> None:
        self.assertIsNotNone(g.guard(payload), payload)

    def assertAllowed(self, payload: dict) -> None:
        self.assertIsNone(g.guard(payload), payload)

    def test_denies_write_shapes(self) -> None:
        for command in (
            "mkdir -p ~/.claude/state/imc",
            "touch ~/.claude/state/imc/F892.waiver.json",
            "env touch $HOME/.claude/state/imc/F892.json",
            "echo '{}' > ~/.claude/state/imc/F892.waiver.json",
            "python3 -c \"import pathlib; pathlib.Path.home().joinpath('.claude/state/imc/F1.json')"
            ".write_text('{}')\"",
            "python3 -c \"from pathlib import Path; (Path.home()/'.claude'/'state'/'imc').mkdir()\"",
            "cat ~/.claude/state/imc/F892.json",
            "rm -rf ~/.claude/state/imc",
        ):
            with self.subTest(command=command):
                self.assertDenied(bash(command))

    def test_denies_file_tools_by_path(self) -> None:
        self.assertDenied({"tool_name": "Write", "tool_input": {
            "file_path": "/home/u/.claude/state/imc/F892.waiver.json", "content": "{}"}})
        self.assertDenied({"tool_name": "Edit", "tool_input": {
            "file_path": "/home/u/.claude/state/imc/F892.json"}})

    def test_file_tool_content_is_not_judged(self) -> None:
        self.assertAllowed({"tool_name": "Write", "tool_input": {
            "file_path": "/repo/tools/hooks/x.py", "content": "Path('~/.claude/state/imc')"}})

    def test_apply_patch_judged_on_its_paths(self) -> None:
        self.assertDenied({"tool_name": "apply_patch", "tool_input": {
            "input": "*** Begin Patch\n*** Add File: /h/.claude/state/imc/F1.json\n+{}\n"}})

    def test_allows_exactly_the_record_command(self) -> None:
        self.assertAllowed(bash("python3 tools/gh/imc_waiver.py record --key F892"))
        self.assertAllowed(bash("python3 /home/u/harmonic-forge/tools/gh/imc_waiver.py record --key F892"))

    def test_record_command_with_anything_chained_is_judged_normally(self) -> None:
        self.assertDenied(bash("python3 tools/gh/imc_waiver.py record --key F892; "
                               "touch ~/.claude/state/imc/F892.waiver.json"))
        self.assertDenied(bash("python3 tools/gh/imc_waiver.py record --key F892 && "
                               "mkdir -p ~/.claude/state/imc"))

    def test_unnormalized_paths_are_denied(self) -> None:
        for path in ("/home/u/.claude/state//imc/F1.waiver.json",
                     "/home/u/.claude/state/./imc/F1.waiver.json",
                     "/home/u/.claude/state/x/../imc/F1.json"):
            self.assertDenied({"tool_name": "Write", "tool_input": {"file_path": path, "content": "{}"}})
            self.assertDenied(bash(f"touch {path}"))

    def test_module_state_writers_are_denied(self) -> None:
        self.assertDenied(bash("python3 -c \"import imc_waiver as w; "
                               "w.write_json_atomic(w.receipt_path('F1'), {})\""))
        self.assertDenied(bash("python3 -c \"import imc_waiver as w; w.receipt_path('F1').unlink()\""))

    def test_record_command_chained_to_anything_is_denied(self) -> None:
        self.assertDenied(bash("python3 tools/gh/imc_waiver.py record --key F892; echo done"))
        self.assertDenied(bash("python3 tools/gh/imc_waiver.py record --key F892 --force"))

    def test_main_fails_closed_when_the_guard_raises(self) -> None:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode()))
        with mock.patch.object(g, "guard", side_effect=RuntimeError("boom")), \
                mock.patch.object(g.sys, "stdin", stdin), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(g.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unrelated_calls_pass(self) -> None:
        self.assertAllowed(bash("ls ~/.claude/state/preclose"))
        self.assertAllowed(bash("git status"))
        self.assertAllowed(bash("grep -n imc tools/hooks/x.py"))

    def test_main_emits_deny_end_to_end(self) -> None:
        done = subprocess.run([sys.executable, str(HERE / "guard_imc_state.py")],
                              input=json.dumps(bash("mkdir -p ~/.claude/state/imc")),
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0)
        out = json.loads(done.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_main_silent_on_allowed(self) -> None:
        done = subprocess.run([sys.executable, str(HERE / "guard_imc_state.py")],
                              input=json.dumps(bash("git status")),
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.stdout.strip(), "")


class RegistrationTests(unittest.TestCase):
    def test_registered_as_pretooluse_in_forge_settings(self) -> None:
        settings = json.loads((HERE.parents[1] / ".claude" / "settings.json").read_text())
        blocks = settings["hooks"]["PreToolUse"]
        hits = [b["matcher"] for b in blocks
                for h in b.get("hooks", []) if "guard_imc_state.py" in h.get("command", "")]
        self.assertEqual(len(hits), 1)
        for tool in ("Bash", "Write", "Edit", "Monitor"):
            self.assertIn(tool, hits[0])


if __name__ == "__main__":
    unittest.main()
