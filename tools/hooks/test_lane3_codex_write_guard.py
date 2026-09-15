#!/usr/bin/env python3
"""Tests for lane3_codex_write_guard.py (harmonic-forge#644).

Exercises the guard through the real subprocess stdin/stdout contract, the
same way Codex actually calls it, rather than importing its internals --
`main()` reading argv/stdin is the surface Codex depends on."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
GUARD = HOOK_DIR / "lane3_codex_write_guard.py"
TESTPLAN = Path.home() / "Harmonic_Projects" / "testplan"


def run_guard(payload: dict, lane: str | None = "3") -> dict:
    env = {}
    import os
    env.update(os.environ)
    if lane is None:
        env.pop("LANE", None)
    else:
        env["LANE"] = lane
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    return json.loads(result.stdout)


def decision(output: dict) -> str:
    return output["hookSpecificOutput"]["permissionDecision"]


def apply_patch_payload(body: str, cwd: str = "/tmp") -> dict:
    return {"tool_name": "apply_patch", "cwd": cwd, "tool_input": {"command": body}}


def bash_payload(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}


class ApplyPatchTests(unittest.TestCase):
    def test_add_inside_testplan_allowed(self):
        body = (
            "*** Begin Patch\n"
            f"*** Add File: {TESTPLAN}/644-probe/x.md\n+hi\n"
            "*** End Patch"
        )
        self.assertEqual(decision(run_guard(apply_patch_payload(body))), "allow")

    def test_update_outside_testplan_denied(self):
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/some-worktree/x.md\n@@\n-old\n+new\n"
            "*** End Patch"
        )
        self.assertEqual(decision(run_guard(apply_patch_payload(body))), "deny")

    def test_delete_outside_testplan_denied(self):
        body = (
            "*** Begin Patch\n"
            "*** Delete File: /tmp/some-worktree/x.md\n"
            "*** End Patch"
        )
        self.assertEqual(decision(run_guard(apply_patch_payload(body))), "deny")

    def test_move_destination_outside_testplan_denied(self):
        body = (
            "*** Begin Patch\n"
            f"*** Update File: {TESTPLAN}/644-probe/x.md\n"
            "*** Move to: /tmp/some-worktree/x.md\n@@\n-old\n+new\n"
            "*** End Patch"
        )
        self.assertEqual(decision(run_guard(apply_patch_payload(body))), "deny")

    def test_malformed_patch_denied(self):
        self.assertEqual(
            decision(run_guard(apply_patch_payload("not a patch at all"))), "deny"
        )

    def test_no_recognized_targets_denied(self):
        body = "*** Begin Patch\n*** End Patch"
        self.assertEqual(decision(run_guard(apply_patch_payload(body))), "deny")


class BashTests(unittest.TestCase):
    def test_redirect_into_testplan_allowed(self):
        cmd = f"echo hi > {TESTPLAN}/644-probe/y.md"
        self.assertEqual(decision(run_guard(bash_payload(cmd, "/tmp"))), "allow")

    def test_redirect_into_worktree_denied(self):
        with tempfile.TemporaryDirectory() as worktree:
            cmd = "echo hi > f.txt"
            self.assertEqual(decision(run_guard(bash_payload(cmd, worktree))), "deny")

    def test_tee_into_worktree_denied(self):
        with tempfile.TemporaryDirectory() as worktree:
            cmd = "echo hi | tee f.txt"
            self.assertEqual(decision(run_guard(bash_payload(cmd, worktree))), "deny")

    def test_cp_into_testplan_allowed(self):
        cmd = f"cp /etc/hostname {TESTPLAN}/644-probe/z.md"
        self.assertEqual(decision(run_guard(bash_payload(cmd, "/tmp"))), "allow")

    def test_read_only_command_allowed(self):
        self.assertEqual(decision(run_guard(bash_payload("git status", "/tmp"))), "allow")

    def test_cd_then_relative_write_into_testplan_allowed(self):
        cmd = f"cd {TESTPLAN}/644-probe && echo hi > z.md"
        self.assertEqual(decision(run_guard(bash_payload(cmd, "/tmp"))), "allow")

    def test_cd_then_relative_write_outside_testplan_denied(self):
        with tempfile.TemporaryDirectory() as worktree:
            cmd = f"cd {worktree} && echo hi > z.md"
            self.assertEqual(decision(run_guard(bash_payload(cmd, "/tmp"))), "deny")

    def test_unresolvable_cd_then_relative_write_denied(self):
        with tempfile.TemporaryDirectory() as worktree:
            cmd = "cd $SOME_VAR && echo hi > z.md"
            self.assertEqual(decision(run_guard(bash_payload(cmd, worktree))), "deny")

    def test_symlink_inside_testplan_pointing_outside_denied(self):
        with tempfile.TemporaryDirectory() as outside:
            probe = TESTPLAN / "644-probe"
            probe.mkdir(parents=True, exist_ok=True)
            link = probe / "escape-link"
            target = Path(outside) / "escaped.md"
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(target)
            try:
                cmd = f"echo hi > {link}"
                self.assertEqual(decision(run_guard(bash_payload(cmd, "/tmp"))), "deny")
            finally:
                link.unlink()

    def test_malformed_shell_denied(self):
        cmd = "echo 'unterminated"
        self.assertEqual(decision(run_guard(bash_payload(cmd, "/tmp"))), "deny")


class LaneGateTests(unittest.TestCase):
    def test_lane2_allows_write_outside_testplan(self):
        with tempfile.TemporaryDirectory() as worktree:
            cmd = "echo hi > f.txt"
            self.assertEqual(
                decision(run_guard(bash_payload(cmd, worktree), lane="2")), "allow"
            )


class ImportGraphTests(unittest.TestCase):
    def test_never_imports_protected_write_denial(self):
        tree = ast.parse(GUARD.read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        self.assertNotIn("protected_write_denial", names)
        # Every call site (not the docstring, which explains the omission
        # in prose) is an ast.Call whose func is a Name/Attribute node --
        # walk those rather than grepping raw source.
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("protected_write_denial", called)


if __name__ == "__main__":
    unittest.main()
