#!/usr/bin/env python3
"""Cross-host and structural contract tests for harmonic-forge#734."""

from __future__ import annotations

import ast
import unittest

from test_lane3_codex_write_guard import (
    GUARD,
    apply_patch_payload,
    bash_payload,
    decision,
    run_guard,
)


class CrossHostDenyTests(unittest.TestCase):
    def test_fail_closed_branches_deny_for_both_hosts(self) -> None:
        cases = (
            bash_payload("echo 'unterminated", "/tmp"),
            bash_payload("cd $SOME_VAR && echo hi > escaped.txt", "/tmp"),
            apply_patch_payload("*** Begin Patch\n*** End Patch", "/tmp"),
            {"tool_name": "Bash"},
            [],
        )
        for host in ("codex", "claude"):
            for payload in cases:
                with self.subTest(host=host, payload=payload):
                    self.assertEqual(decision(run_guard(payload, host=host)), "deny")


class SharedAllowDecisionTests(unittest.TestCase):
    def test_every_permissive_site_calls_the_shared_host_decision(self) -> None:
        tree = ast.parse(GUARD.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_allow"
        ]
        self.assertEqual(len(calls), 4)
        for call in calls:
            self.assertEqual(len(call.args), 1)
            self.assertIsInstance(call.args[0], ast.Name)
            self.assertEqual(call.args[0].id, "host")


if __name__ == "__main__":
    unittest.main()
