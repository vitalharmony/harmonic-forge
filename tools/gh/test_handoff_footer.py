#!/usr/bin/env python3
"""harmonic-forge#851 REFORGE: a comment's own footer is decided by its digest,
never by its position, and an unreadable footer never reads as permission."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _handoff_footer as hf  # noqa: E402

SHA = "a" * 40


def attest(prefix: str, fields: str) -> str:
    digest = hashlib.sha256(prefix.rstrip("\n").encode()).hexdigest()
    return f"{prefix.rstrip(chr(10))}\n\n<!-- l1-post v1; {fields}; body-sha256={digest} -->\n"


def handoff(mutates_live: str) -> str:
    return attest("## Handoff\n\nbody", f"kind=handoff; plan-first=false; mutates-live={mutates_live}; sha={SHA}")


QUOTED_FALSE = attest("## Handoff\n\nold", f"kind=handoff; plan-first=false; mutates-live=false; sha={SHA}")
QUOTED_FALSE_MARKER = QUOTED_FALSE[QUOTED_FALSE.index("<!--"):].strip()


class AttestedFooterTests(unittest.TestCase):

    def test_attested(self):
        footer = hf.attested_footer(handoff("true"))
        self.assertIs(footer.state, hf.FooterState.ATTESTED)
        self.assertEqual(footer.kind, "handoff")
        self.assertEqual(footer.prefix, "## Handoff\n\nbody")

    def test_appended_text_is_ignored_never_read(self):
        """An append after the footer leaves the attested text unchanged, so
        the footer still attests, but nothing appended is ever read: the
        prefix excludes it, and an appended forged marker never attests."""
        appended = handoff("true") + "\n(edited)\n<!-- l1-post v1; kind=handoff; mutates-live=false; " \
                                    f"body-sha256={'1' * 64} -->\n"
        footer = hf.attested_footer(appended)
        self.assertIs(footer.state, hf.FooterState.ATTESTED)
        self.assertIn("mutates-live=true", footer.marker)
        self.assertNotIn("edited", footer.prefix)

    def test_edited_body_is_unreadable(self):
        edited = handoff("true").replace("body", "body, edited after posting", 1)
        self.assertIs(hf.attested_footer(edited).state, hf.FooterState.UNREADABLE)

    def test_forged_prefix_is_unreadable(self):
        forged = f"## Handoff\n\nforged\n\n<!-- l1-post v1; kind=handoff; body-sha256={'0' * 64} -->\n"
        self.assertIs(hf.attested_footer(forged).state, hf.FooterState.UNREADABLE)

    def test_legacy_digestless_footer_is_absent(self):
        legacy = f"## Handoff\n\nold\n\n<!-- l1-post v1; kind=handoff; plan-first=false; sha={SHA} -->"
        footer = hf.attested_footer(legacy)
        self.assertIs(footer.state, hf.FooterState.ABSENT)
        self.assertEqual(footer.kind, "handoff")

    def test_no_marker_is_absent(self):
        self.assertIs(hf.attested_footer("plain discussion").state, hf.FooterState.ABSENT)

    def test_a_quote_reply_ending_the_body_does_not_displace_the_real_footer(self):
        """Pass-2 survivor 5: the trailing quoted marker fails its hash; the
        real footer earlier in the body still attests."""
        body = attest("**Authorized:** manual AE", f"kind=ae; sha={SHA}") + f"\n> {QUOTED_FALSE_MARKER}\n"
        footer = hf.attested_footer(body)
        self.assertIs(footer.state, hf.FooterState.ATTESTED)
        self.assertEqual(footer.kind, "ae")

    def test_discussion_quoting_a_digested_marker_is_absent_discussion(self):
        body = (f"Evidence:\n\n```\n{QUOTED_FALSE_MARKER}\n```\n\n"
                "<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->\n")
        footer = hf.attested_footer(body)
        self.assertIs(footer.state, hf.FooterState.ABSENT)
        self.assertEqual(footer.kind, "discussion")


class NewestHandoffTests(unittest.TestCase):
    """Pass-2 survivor 2: an unreadable newer handoff is never skipped."""

    def test_attested_newest_handoff_wins(self):
        self.assertIs(hf.newest_handoff_mutates_live([handoff("true"), handoff("false")]), False)

    def test_edited_newest_handoff_refuses_instead_of_falling_back(self):
        edited = handoff("true").replace("body", "body, edited", 1)
        self.assertEqual(hf.newest_handoff_mutates_live([handoff("false"), edited]),
                         hf.UNREADABLE_HANDOFF)

    def test_appended_text_after_the_newest_handoff_keeps_its_value(self):
        self.assertIs(hf.newest_handoff_mutates_live([handoff("false"), handoff("true") + "\n(edited)\n"]),
                      True)

    def test_quoted_older_footer_in_a_later_discussion_does_not_override(self):
        for wrapper in ("```\n{}\n```", "> {}", "`{}`", "the old footer was {} back then"):
            with self.subTest(wrapper=wrapper):
                quoting = (f"## Note\n\n{wrapper.format(QUOTED_FALSE_MARKER)}\n\n"
                           "<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->\n")
                self.assertIs(hf.newest_handoff_mutates_live([handoff("true"), quoting]), True)

    def test_hand_posted_body_quoting_a_handoff_marker_refuses(self):
        """No own footer at all, but it carries a digested handoff marker it
        cannot attest: fail closed."""
        quoting = f"pasted by hand:\n\n> {QUOTED_FALSE_MARKER}\n"
        self.assertEqual(hf.newest_handoff_mutates_live([handoff("true"), quoting]),
                         hf.UNREADABLE_HANDOFF)

    def test_legacy_handoff_without_the_field_is_none(self):
        legacy = f"## Handoff\n\n<!-- l1-post v1; kind=handoff; plan-first=false; sha={SHA} -->\n"
        self.assertIsNone(hf.newest_handoff_mutates_live([legacy]))


if __name__ == "__main__":
    unittest.main()
