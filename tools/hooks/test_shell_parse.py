"""harmonic-forge#785: `strip_invocation_prefix`'s #782 shell unwrap is opt-out.

Gate classifiers want `bash -c "<script>"` unwrapped to the command inside;
write guards must see the nested shell itself, or their nested-shell rules
never fire.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
from shell_parse import strip_invocation_prefix  # noqa: E402

NESTED = ["bash", "-c", "echo x > y"]


class UnwrapShellsTests(unittest.TestCase):
    def test_default_unwraps_a_nested_shell(self):
        self.assertEqual(strip_invocation_prefix(list(NESTED)), ["echo", "x", ">", "y"])

    def test_unwrap_shells_false_leaves_the_nested_shell_intact(self):
        self.assertEqual(strip_invocation_prefix(list(NESTED), unwrap_shells=False), NESTED)

    def test_unwrap_shells_false_still_strips_gh_as(self):
        self.assertEqual(
            strip_invocation_prefix(["gh-as", "vitalharmony", "gh", "pr", "view"], unwrap_shells=False),
            ["gh", "pr", "view"],
        )


if __name__ == "__main__":
    unittest.main()
