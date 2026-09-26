#!/usr/bin/env python3
"""Unit tests for deny_retired_belt_sweep.py (harmonic-forge#766).
Run: python3 tools/hooks/test_deny_retired_belt_sweep.py"""

import json
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import deny_retired_belt_sweep as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class DecisionTests(unittest.TestCase):
    def test_bare_invocation_is_denied(self):
        result = m.decision("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --sweep-for l1")
        self.assertTrue(_is_denied(result))

    def test_l3_value_is_also_denied(self):
        """The flag itself is the trigger, not a particular value -- both
        values were separately retired (harmonic-forge#640, #659), and the
        flag no longer exists in the parser at all."""
        result = m.decision("python3 tools/gh/watch_lane_posts.py --sweep-for l3 --account-repos vitalharmony")
        self.assertTrue(_is_denied(result))

    def test_chained_after_other_commands_is_still_caught(self):
        result = m.decision("cd /tmp && python3 tools/gh/watch_lane_posts.py --sweep-for l1 --repo o/r")
        self.assertTrue(_is_denied(result))

    def test_the_message_never_echoes_the_token(self):
        """The whole point: a refusal that quotes the command back re-teaches
        it -- the exact mechanism harmonic-forge#766 exists to stop."""
        result = m.decision("python3 tools/gh/watch_lane_posts.py --sweep-for l1")
        message = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertNotIn("--sweep-for", message)
        self.assertIn("belt_plan.py", message)

    def test_ordinary_belt_command_is_untouched(self):
        result = m.decision(
            "python3 tools/gh/watch_lane_posts.py --queue-for l1 --repo o/r --watch l2"
        )
        self.assertEqual(result, {})

    def test_unrelated_command_is_untouched(self):
        self.assertEqual(m.decision("git status"), {})

    def test_a_flag_that_merely_starts_with_the_same_prefix_is_untouched(self):
        """`\\b` word-boundary: `--sweep-forever` (hypothetical future flag)
        must not collide with the retired token."""
        result = m.decision("python3 tools/gh/watch_lane_posts.py --sweep-forever")
        self.assertEqual(result, {})


class MainTests(unittest.TestCase):
    def _run(self, payload: dict) -> dict:
        with patch("sys.stdin", StringIO(json.dumps(payload))), \
             patch("sys.stdout", new_callable=StringIO) as out:
            m.main()
        return json.loads(out.getvalue())

    def test_non_bash_tool_passes_through(self):
        result = self._run({"tool_name": "Read", "tool_input": {"command": "--sweep-for l1"}})
        self.assertEqual(result, {})

    def test_bash_with_retired_flag_is_denied(self):
        result = self._run({"tool_name": "Bash",
                            "tool_input": {"command": "python3 x.py --sweep-for l1"}})
        self.assertTrue(_is_denied(result))

    def test_malformed_stdin_fails_open(self):
        with patch("sys.stdin", StringIO("not json")), \
             patch("sys.stdout", new_callable=StringIO) as out:
            m.main()
        self.assertEqual(json.loads(out.getvalue()), {})


if __name__ == "__main__":
    unittest.main()
