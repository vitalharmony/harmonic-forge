#!/usr/bin/env python3
"""Tests for block_unbounded_poll (harmonic-forge#948 AC3)."""
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_unbounded_poll as H  # noqa: E402


def decision(command: str, tool: str = "Bash") -> str | None:
    out = io.StringIO()
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    with unittest.mock.patch.object(sys, "stdin", io.StringIO(payload)), \
         unittest.mock.patch.object(sys, "stdout", out):
        H.main()
    return json.loads(out.getvalue()).get("hookSpecificOutput", {}).get("permissionDecision")


class Polls(unittest.TestCase):
    def test_the_h2245_loop_is_denied(self):
        self.assertEqual(decision('until grep -q "^exit=" out.log; do sleep 10; done'), "deny")

    def test_a_while_poll_across_lines_is_denied(self):
        self.assertEqual(decision("while pgrep -f job >/dev/null\ndo\n  sleep 15\ndone; echo ok"), "deny")

    def test_a_loop_under_timeout_is_allowed(self):
        self.assertIsNone(decision("timeout 600 bash -c 'until grep -q x f; do sleep 10; done'"))

    def test_a_while_read_loop_without_sleep_is_allowed(self):
        self.assertIsNone(decision("ls | while read f; do echo $f; done"))

    def test_sleep_alone_is_allowed(self):
        self.assertIsNone(decision("sleep 5"))

    def test_a_timeout_after_the_loop_does_not_count(self):
        self.assertEqual(decision("until false; do sleep 1; done; timeout 5 true"), "deny")

    # Preclose pass 1 survivors (reforged onto shell_parse), one test each.
    def test_an_earlier_unrelated_timeout_does_not_exempt_the_loop(self):
        self.assertEqual(decision("timeout 5 true; until false; do sleep 1; done"), "deny")
        self.assertEqual(decision("curl --connect-timeout 5 x; until false; do sleep 1; done"), "deny")

    def test_a_nested_loop_does_not_hide_the_outer_sleep(self):
        self.assertEqual(decision("until a; do until b; do :; done; sleep 5; done"), "deny")

    def test_a_sleep_in_the_loop_condition_is_caught(self):
        self.assertEqual(decision("while sleep 10; do grep -q x f && break; done"), "deny")

    def test_quoted_prose_is_not_a_loop(self):
        self.assertIsNone(decision('git commit -m "wait until ready; do not sleep forever; done"'))

    def test_a_bare_bash_c_loop_is_checked(self):
        self.assertEqual(decision("bash -c 'until x; do sleep 1; done'"), "deny")

    def test_large_input_is_decided_quickly(self):
        import time
        start = time.monotonic()
        decision("while x; do " * 3000)
        self.assertLess(time.monotonic() - start, 2)

    def test_a_non_object_payload_is_allowed_not_crashed(self):
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO("[]")), \
             unittest.mock.patch.object(sys, "stdout", out):
            H.main()
        self.assertEqual(json.loads(out.getvalue()), {})

    # Reforge pass 1 survivors.
    def test_a_loop_after_a_compound_keyword_is_caught(self):
        self.assertEqual(decision("if true; then while :; do sleep 1; done; fi"), "deny")
        self.assertEqual(decision("{ until x; do sleep 1; done; }"), "deny")

    def test_every_leader_keyword_is_stripped(self):
        # Reforge pass 2: each LEADERS entry needs its own failing case.
        for command in ("if a; then :; else until b; do sleep 1; done; fi",
                        "if a; then :; elif b; then while :; do sleep 1; done; fi",
                        "! until x; do sleep 1; done",
                        "until x; do exec sleep 1; done"):
            with self.subTest(command=command):
                self.assertEqual(decision(command), "deny")

    def test_sleep_by_path_or_builtin_prefix_counts(self):
        self.assertEqual(decision("until [ -f x ]; do /bin/sleep 5; done"), "deny")
        self.assertEqual(decision("until x; do command sleep 1; done"), "deny")

    def test_a_combined_shell_flag_is_checked(self):
        self.assertEqual(decision("bash -lc 'until x; do sleep 1; done'"), "deny")

    def test_split_flags_and_other_shells_are_checked(self):
        for command in ('bash -x -c "until f; do sleep 1; done"',
                        "zsh -c 'until f; do sleep 1; done'",
                        "dash -c 'while :; do sleep 1; done'"):
            with self.subTest(command=command):
                self.assertEqual(decision(command), "deny")
        self.assertIsNone(decision("bash -x script.sh"))

    def test_other_tools_are_ignored(self):
        self.assertIsNone(decision("until false; do sleep 1; done", tool="Read"))

    def test_the_reason_names_both_fixes(self):
        self.assertIn("timeout 1800", H.REASON)
        self.assertIn("background", H.REASON)


if __name__ == "__main__":
    unittest.main()
