#!/usr/bin/env python3
"""harmonic-forge#1319: a Codex `gh pr merge` reaches the route recheck.

Codex stores hook trust per handler position, so the merge hook is appended as
the last handler of the `^Bash$` block: no existing handler moves off the
position its trust was minted for.
"""
import json
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / ".codex" / "hooks.json"
SCRIPT = "tools/hooks/block_route_tightened_merge.py"


class CodexRouteRecheckWiring(unittest.TestCase):
    def test_the_merge_hook_is_the_last_bash_handler(self):
        blocks = json.loads(HOOKS.read_text())["hooks"]["PreToolUse"]
        bash = [b for b in blocks if b.get("matcher") == "^Bash$"]
        self.assertEqual(len(bash), 1)
        commands = [h["command"] for h in bash[0]["hooks"]]
        self.assertIn(SCRIPT, commands[-1])
        self.assertEqual(sum(SCRIPT in c for c in commands), 1)


if __name__ == "__main__":
    unittest.main()
