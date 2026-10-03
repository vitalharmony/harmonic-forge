#!/usr/bin/env python3
"""harmonic-forge#878: no tool call touches the gate-prod receipt store."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import deny_gate_prod_store_write as m  # noqa: E402

STORE = str(m.STORE)


class BashTests(unittest.TestCase):
    def test_a_command_naming_the_store_is_denied(self):
        for command in (f"rm -f {STORE}/abc.json",
                        "rm -rf ~/.claude/state/gate-prod",
                        "rm -rf $HOME/.claude/state/gate-prod/*",
                        "cd ~/.claude/state && rm -r gate-prod",
                        "find ~/.claude/state/'gate-prod' -delete",
                        "mv ~/.claude/state/gate_prod /tmp/x",
                        "cat ~/.claude/state/gate-prod/x.json"):
            with self.subTest(command=command):
                self.assertEqual(m.denial_reason("Bash", {"command": command}), m.REASON)

    def test_unrelated_commands_pass(self):
        for command in ("ls ~/.claude/state", "backend/.venv/bin/python scripts/gate_production_run.py "
                        "--issue 1867 --count-label Interpretation", "git status"):
            with self.subTest(command=command):
                self.assertIsNone(m.denial_reason("Bash", {"command": command}))

    def test_a_malformed_command_fails_closed(self):
        self.assertEqual(m.denial_reason("Bash", {"command": None}), m.REASON)


class PathToolTests(unittest.TestCase):
    def test_a_write_into_the_store_is_denied(self):
        for tool, key, path in (("Write", "file_path", f"{STORE}/k.json"),
                                ("Edit", "file_path", f"{STORE}/k.json"),
                                ("MultiEdit", "file_path", STORE),
                                ("NotebookEdit", "notebook_path", f"{STORE}/../gate-prod/x")):
            with self.subTest(tool=tool, path=path):
                self.assertEqual(m.denial_reason(tool, {key: path}), m.REASON)

    def test_a_write_elsewhere_passes(self):
        self.assertIsNone(m.denial_reason("Write", {"file_path": f"{STORE}-other/x"}))
        self.assertIsNone(m.denial_reason("Edit", {"file_path": "/tmp/gate-prod/x"}))
        self.assertIsNone(m.denial_reason("Read", {"file_path": f"{STORE}/k.json"}))


class EntryPointTests(unittest.TestCase):
    def test_main_prints_a_deny(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": f"rm {STORE}/k"}})
        out = subprocess.run([sys.executable, str(Path(m.__file__))], input=payload,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
