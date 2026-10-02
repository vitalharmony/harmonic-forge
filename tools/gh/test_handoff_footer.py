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


def handoff(mutates_live: str, note: str = "") -> str:
    # Text is unique per value (and note): identical attested text in two
    # comments is a replay, and the later one is refused.
    return attest(f"## Handoff\n\nbody ({mutates_live}{note})",
                  f"kind=handoff; plan-first=false; mutates-live={mutates_live}; sha={SHA}")


QUOTED_FALSE = attest("## Handoff\n\nold", f"kind=handoff; plan-first=false; mutates-live=false; sha={SHA}")
QUOTED_FALSE_MARKER = QUOTED_FALSE[QUOTED_FALSE.index("<!--"):].strip()


class AttestedFooterTests(unittest.TestCase):

    def test_attested(self):
        footer = hf.attested_footer(handoff("true"))
        self.assertIs(footer.state, hf.FooterState.ATTESTED)
        self.assertEqual(footer.kind, "handoff")
        self.assertEqual(footer.prefix, "## Handoff\n\nbody (true)")

    def test_appended_plain_text_is_ignored_never_read(self):
        """Plain text appended after the footer leaves the attested text
        unchanged, so the footer still attests, and nothing appended is read."""
        footer = hf.attested_footer(handoff("true") + "\n(edited)\n")
        self.assertIs(footer.state, hf.FooterState.ATTESTED)
        self.assertIn("mutates-live=true", footer.marker)
        self.assertNotIn("edited", footer.prefix)

    def test_an_appended_forged_marker_makes_the_body_unreadable(self):
        """The last marker is the only candidate, so an appended forged footer
        is what is read -- and it does not attest."""
        appended = handoff("true") + "\n<!-- l1-post v1; kind=handoff; mutates-live=false; " \
                                    f"body-sha256={'1' * 64} -->\n"
        self.assertIs(hf.attested_footer(appended).state, hf.FooterState.UNREADABLE)

    def test_edited_body_is_unreadable(self):
        edited = handoff("true").replace("body", "body, edited after posting", 1)
        self.assertIs(hf.attested_footer(edited).state, hf.FooterState.UNREADABLE)

    def test_forged_prefix_is_unreadable(self):
        forged = f"## Handoff\n\nforged\n\n<!-- l1-post v1; kind=handoff; body-sha256={'0' * 64} -->\n"
        self.assertIs(hf.attested_footer(forged).state, hf.FooterState.UNREADABLE)

    def test_legacy_digestless_footer_is_absent_and_names_no_kind(self):
        """Reforge pass 1 survivor 1: a digest-less marker -- quoted or not --
        never nominates a kind."""
        legacy = f"## Handoff\n\nold\n\n<!-- l1-post v1; kind=handoff; plan-first=false; sha={SHA} -->"
        footer = hf.attested_footer(legacy)
        self.assertIs(footer.state, hf.FooterState.ABSENT)
        self.assertIsNone(footer.kind)

    def test_no_marker_is_absent(self):
        self.assertIs(hf.attested_footer("plain discussion").state, hf.FooterState.ABSENT)

    def test_a_quote_reply_ending_the_body_is_unreadable_never_the_quoted_kind(self):
        """The last marker is the only candidate: a trailing quoted footer
        fails its hash, so the body is UNREADABLE (refused), and the quote
        never becomes the body's state. No search falls back to an earlier
        marker (reforge pass 1 survivor 2)."""
        body = attest("**Authorized:** manual AE", f"kind=ae; sha={SHA}") + f"\n> {QUOTED_FALSE_MARKER}\n"
        self.assertIs(hf.attested_footer(body).state, hf.FooterState.UNREADABLE)

    def test_a_broken_own_footer_never_falls_through_to_an_embedded_attested_one(self):
        older = attest("## Sweep\n\nWrite tier: R", f"kind=sweep; sha={SHA}")
        edited = f"{older}\nlater edit\n\n<!-- l1-post v1; kind=sweep; sha={SHA}; body-sha256={'2' * 64} -->\n"
        self.assertIs(hf.attested_footer(edited).state, hf.FooterState.UNREADABLE)

    def test_discussion_quoting_a_digested_marker_is_absent_discussion(self):
        body = (f"Evidence:\n\n```\n{QUOTED_FALSE_MARKER}\n```\n\n"
                "<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->\n")
        footer = hf.attested_footer(body)
        self.assertIs(footer.state, hf.FooterState.ABSENT)
        self.assertIsNone(footer.kind)


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

    def test_a_verbatim_replay_of_an_older_handoff_is_refused(self):
        """Reforge pass 1 survivor 3: a byte-identical paste of an earlier
        handoff re-attests, so first attestation wins and the copy is
        UNREADABLE -- it can never stand in as the newest handoff."""
        older_false = handoff("false")
        self.assertEqual(hf.newest_handoff_mutates_live([older_false, handoff("true"), older_false]),
                         hf.UNREADABLE_HANDOFF)

    def test_quoted_digestless_handoff_marker_never_counts(self):
        """Reforge pass 1 survivor 1."""
        quoting = f"see:\n\n> <!-- l1-post v1; kind=handoff; mutates-live=false; sha={SHA} -->\n"
        self.assertIs(hf.newest_handoff_mutates_live([handoff("true"), quoting]), True)

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
