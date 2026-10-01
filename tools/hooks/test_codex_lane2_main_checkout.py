#!/usr/bin/env python3
"""harmonic-forge#840: Codex Lane 2 is kept out of the main checkout.

Runs the real `lane3_codex_write_guard.py` as a subprocess with Codex's own
PreToolUse payload shapes (`tool_name` `Bash` / `apply_patch`, the command in
`tool_input.command`, patch targets on `*** Add/Update/Delete File:` lines),
the shapes #644 and #734 verified against codex-cli. No `--host`, exactly as
every `.codex/hooks.json` wires it, so an allow is silence.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from test_lane3_codex_write_guard import apply_patch_payload, bash_payload, decision, run_guard


def patch(op: str, path: Path) -> str:
    return f"*** Begin Patch\n*** {op} File: {path}\n+x\n*** End Patch"


class CodexLane2MainCheckout(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.main = base / "proj"
        self.main.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.main)], check=True)
        (self.main / "tracked.md").write_text("x\n")
        subprocess.run(["git", "-C", str(self.main), "add", "tracked.md"], check=True)
        subprocess.run(["git", "-C", str(self.main), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed"], check=True)
        self.impl = base / "proj-42-impl"
        subprocess.run(["git", "-C", str(self.main), "worktree", "add", "-q", "-b", "work",
                        str(self.impl)], check=True)

    def lane2(self, payload: dict) -> str | None:
        return decision(run_guard(payload, lane="2"))

    def test_apply_patch_into_the_main_checkout_is_denied(self) -> None:
        for op in ("Add", "Update", "Delete"):
            with self.subTest(op=op):
                self.assertEqual(self.lane2(apply_patch_payload(
                    patch(op, self.main / "tracked.md"), str(self.impl))), "deny")

    def test_apply_patch_inside_the_impl_worktree_is_allowed_silently(self) -> None:
        self.assertIsNone(self.lane2(apply_patch_payload(
            patch("Update", self.impl / "tracked.md"), str(self.impl))))

    def test_shell_writes_into_the_main_checkout_are_denied(self) -> None:
        for command in (f"echo x > {self.main}/tracked.md",
                        f"cd {self.main} && echo x > tracked.md",
                        f"cp a.txt {self.main}/b.txt"):
            with self.subTest(command=command):
                self.assertEqual(self.lane2(bash_payload(command, str(self.impl))), "deny")

    def test_shell_writes_inside_the_impl_worktree_are_allowed_silently(self) -> None:
        for command in ("echo x > tracked.md", f"echo x > {self.impl}/new.md", "ls -la"):
            with self.subTest(command=command):
                self.assertIsNone(self.lane2(bash_payload(command, str(self.impl))))

    def test_lane1_is_not_guarded(self) -> None:
        """Lane 1 works in the main checkout by design."""
        out = run_guard(apply_patch_payload(patch("Update", self.main / "tracked.md"),
                                            str(self.main)), lane="1")
        self.assertIsNone(decision(out))

    def test_lane2_fails_open_on_an_unparseable_payload(self) -> None:
        """An accidental-mix-up guard: a parse gap never blocks Lane 2's own work."""
        for payload in ({"tool_name": "Bash"}, [],
                        apply_patch_payload("not a patch", str(self.impl))):
            with self.subTest(payload=payload):
                self.assertIsNone(self.lane2(payload))


if __name__ == "__main__":
    unittest.main()
