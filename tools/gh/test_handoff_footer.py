#!/usr/bin/env python3
"""harmonic-forge#851 sticky-wicket PATCH: the handoff footer reader reads only a
body's own trailing footer (preclose pass 1 survivor 3)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _handoff_footer as hf  # noqa: E402

SHA = "a" * 40


def handoff(mutates_live: str) -> str:
    return (f"## Handoff\n\nbody\n\n<!-- l1-post v1; kind=handoff; plan-first=false; "
            f"mutates-live={mutates_live}; sha={SHA} -->\n")


QUOTED_FALSE = f"<!-- l1-post v1; kind=handoff; plan-first=false; mutates-live=false; sha={SHA} -->"
REWORK_FOOTER = f"<!-- l1-post v1; kind=rework; sha={SHA} -->"


class TrailingFooterTests(unittest.TestCase):

    def test_returns_the_last_marker_when_it_ends_the_body(self):
        self.assertIn("kind=handoff", hf.trailing_footer(handoff("true")))

    def test_a_marker_followed_by_prose_is_not_a_footer(self):
        self.assertIsNone(hf.trailing_footer(f"{QUOTED_FALSE}\n\nprose after it"))

    def test_no_marker_is_none(self):
        self.assertIsNone(hf.trailing_footer("plain discussion"))


class QuotedHandoffFooterTests(unittest.TestCase):
    """A later comment quoting an older `mutates-live=false` footer must never
    override the real newest handoff's `mutates-live=true`."""

    WRAPPERS = {"fence": "```\n{}\n```", "blockquote": "> {}", "inline": "`{}`",
                "prose": "the old footer was {} back then"}

    def test_quoted_footer_in_a_later_comment_does_not_override(self):
        for name, wrapper in self.WRAPPERS.items():
            with self.subTest(wrapper=name):
                quoting = f"## Rework\n\n{wrapper.format(QUOTED_FALSE)}\n\n{REWORK_FOOTER}\n"
                self.assertIs(hf.newest_handoff_mutates_live([handoff("true"), quoting]), True)

    def test_quoting_comment_with_no_trailing_footer_is_ignored(self):
        quoting = f"discussion\n\n> {QUOTED_FALSE}\n\nmore"
        self.assertIs(hf.newest_handoff_mutates_live([handoff("true"), quoting]), True)

    def test_a_real_newer_handoff_still_wins(self):
        self.assertIs(hf.newest_handoff_mutates_live([handoff("true"), handoff("false")]), False)

    def test_legacy_handoff_without_the_field_is_none(self):
        legacy = f"## Handoff\n\n<!-- l1-post v1; kind=handoff; plan-first=false; sha={SHA} -->\n"
        self.assertIsNone(hf.newest_handoff_mutates_live([legacy]))


if __name__ == "__main__":
    unittest.main()
