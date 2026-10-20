"""Tests for the shared visible-text reader (harmonic-forge#1103 reforge).

Each case is a finding from #1103's preclose passes 1..4. The table is run
twice: against `markdown_strip.visible_text` directly, and through
`watch_lane_posts._is_gate_report`, the belt's recognizer that delegates to it
-- so a regression in either the reader or the delegation fails here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch_lane_posts  # noqa: E402

H = "## Lane 3 Gate Results — H1 — FAIL"
F = "````"

#: (name, body, gate heading visible?)
CASES = [
    ("tilde fence hides a heading", f"note\n~~~\n{H}\n~~~\n", False),
    ("unclosed fence hides to end", f"note\n```\n{H}\n", False),
    ("4-backtick fence containing ``` stays open",
     f"note\n{F}\n```\n{H}\n```\n{F}\n", False),
    ("shorter closer does not close", f"```` \n{H}\n```\n", False),
    ("closer with trailing text does not close",
     f"```\nx\n``` not a closer\n{H}\n", False),
    ("tilde closer does not close a backtick fence",
     f"```\n~~~\n{H}\n", False),
    ("heading after a closed fence is visible", f"```\nx\n```\n{H}\n", True),
    ("line-start inline ``` span is not a fence",
     f"```code``` is inline\n{H}\n", True),
    ("info string with a backtick is not a fence",
     f"```py`x\n{H}\n", True),
    ("4-space indented code is not a fence opener",
     f"note\n\n    ```\n\n{H}\n", True),
    ("4-space indented heading after a blank is code",
     f"note\n\n    {H}\n", False),
    ("4-space line continuing a paragraph is text",
     f"{H}\n    continuation\n", True),
    ("quoted heading is hidden", f"> {H}\n", False),
    ("indented quoted heading is hidden", f"  > {H}\n", False),
    ("prefaced heading is visible", f"Lane 1 relay:\n\n{H}\n", True),
    ("fence indented 3 spaces is a fence", f"   ```\n{H}\n", False),
]


class VisibleTextTableTests(unittest.TestCase):
    def test_table(self):
        import markdown_strip
        for name, body, visible in CASES:
            with self.subTest(name):
                self.assertEqual(H in markdown_strip.visible_text(body), visible)

    def test_inline_spans_are_kept_as_text(self):
        import markdown_strip
        self.assertEqual(markdown_strip.visible_text("a `b` c\n"), "a `b` c\n")


class BeltRecognizerTableTests(unittest.TestCase):
    """The same table through the belt's own recognizer (first visible line)."""

    def test_table(self):
        for name, body, visible in CASES:
            stripped = watch_lane_posts._strip_quoted(body)
            with self.subTest(name):
                self.assertEqual(H in stripped, visible)


if __name__ == "__main__":
    unittest.main()
