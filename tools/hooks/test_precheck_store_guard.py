"""harmonic-forge#1291 design alternative 8: the precheck gate's store guard."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import precheck_store_guard as guard  # noqa: E402

STORE = "~/.claude/state/precheck-gate"


def bash(command: str) -> str | None:
    return guard.guard({"tool_name": "Bash", "tool_input": {"command": command}})


def write(tool: str, path: str, content: str = "x") -> str | None:
    return guard.guard({"tool_name": tool, "tool_input": {"file_path": path, "content": content}})


class Guard(unittest.TestCase):
    def test_write_into_store_denied(self) -> None:
        self.assertIsNotNone(write("Write", f"{STORE}/x.json"))
        self.assertIsNotNone(write("Edit", "/home/u/.claude/state/precheck_gate/s/x.json"))
        self.assertIsNotNone(bash(f"echo '{{}}' > {STORE}/abc/x.json"))
        self.assertIsNotNone(bash(f"rm -rf {STORE}"))
        self.assertIsNotNone(bash(f"cp /tmp/x {STORE}/refusals/aaaaaaaaaaaa.json"))

    def test_inline_record_denied(self) -> None:
        self.assertIsNotNone(bash('python3 -c "import gate_receipt; gate_receipt.record(\'t\', [])"'))
        self.assertIsNotNone(bash("python3 - <<'EOF'\nimport gate_receipt\ngate_receipt.record('t', [])\nEOF"))
        self.assertIsNotNone(bash("python3 tools/precheck/gate_receipt.py record --tool tools/lane/lanes-claude"))

    def test_reads_of_state_dir_allowed(self) -> None:
        for command in (f"rg -n x {STORE}/decisions.jsonl", f"grep refused {STORE}/decisions.jsonl",
                        f"cat {STORE}/decisions.jsonl", f"less {STORE}/decisions.jsonl",
                        f"ls -la {STORE}", f"jq . {STORE}/decisions.jsonl",
                        f"cat {STORE}/decisions.jsonl | jq .outcome"):
            self.assertIsNone(bash(command), command)
        self.assertIsNotNone(bash(f"rg --pre sh x {STORE}/decisions.jsonl"))
        self.assertIsNotNone(bash(f"rg --pre=sh x {STORE}"))
        self.assertIsNotNone(bash(f"cat {STORE}/decisions.jsonl > /tmp/x"))
        self.assertIsNotNone(bash(f"cat {STORE}/decisions.jsonl; rm {STORE}/x"))

    def test_the_gate_and_its_own_files_are_not_the_store(self) -> None:
        for path in ("tools/hooks/precheck_gate.py", "tools/precheck/gate_receipt.py",
                     "tools/hooks/test_precheck_store_guard.py", "tools/precheck/gate.py"):
            self.assertIsNone(write("Edit", path, "gate_receipt.record(...) ~/.claude/state/precheck-gate"), path)
        self.assertIsNone(bash("python3 -m pytest -q tools/precheck/test_gate.py"))
        self.assertIsNone(bash("python3 tools/hooks/precheck_gate.py < payload.json"))

    def test_unrelated_commands_pass(self) -> None:
        self.assertIsNone(bash("git status"))
        self.assertIsNone(write("Write", "/tmp/notes.md", "precheck-gate"))

    def test_hook_entry_denies_as_json(self) -> None:
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": f"touch {STORE}/x"}})
        done = subprocess.run([sys.executable, str(HERE / "precheck_store_guard.py")], input=payload,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
