#!/usr/bin/env python3
"""Mechanical assertions over SKILL.md (harmonic-forge#518 AC2, AC10).

AC2 exists because the defect it targets survived a human reading past it: the
Documents file's Lane 2 section ends "My role in this loop is limited to **Lane
1** review, handoff, and AE-and-sweep work," and also grants merge-and-close.
A reviewer read that and did not see it.

**Why this is not a substring search.** The Lane 2 section legitimately names
`ae-and-sweep` — in its list of marker kinds the belt stays *silent* on, which
is the opposite of claiming the authority. A naive `"ae-and-sweep" in lane2`
check fails on correct text, and the predictable response to a test that cries
wolf is to weaken it until it means nothing. So the enumeration of marker kinds
is excluded, and what is asserted is an authority *grant*.
"""

import re
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).parent / "SKILL.md"

#: The kinds enumeration — marker names as data, not as authority. Anything on
#: this line is a `kind=` value the belt filters on.
_KINDS_LINE = re.compile(
    r"^Fires on .*?\.\s*Silent on .*?$|^channel\.$", re.MULTILINE | re.DOTALL
)

#: Verbs that would grant Lane 2 something only Lane 1 may do.
_AUTHORITY = (
    "merge and close",
    "merge the pr",
    "close the issue",
    "may merge",
    "may close",
    "post an ae",
    "post a sweep",
    "ae-and-sweep work",
    "lane 1 review, handoff",
)


def _norm(text: str) -> str:
    """Lowercase, drop markdown emphasis, collapse whitespace.

    Asserting exact substrings against hard-wrapped prose is brittle in a way
    that fails *open*: `merge and\\nclose directly` does not contain "merge and
    close directly", so a naive check would report the authority absent from
    Lane 1 — and would equally miss it if it appeared in Lane 2. Both directions
    matter, so normalize before matching.
    """
    # `*` only. Stripping `_` as emphasis destroys identifiers —
    # `assert_identity` becomes `assertidentity` and `Harmonic_Projects`
    # becomes `harmonicprojects`, so assertions about code names silently stop
    # matching. This document uses `*` for emphasis throughout.
    return re.sub(r"\s+", " ", text.replace("*", "")).lower()


def _section(name: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index(f"## Role: {name}")
    rest = text[start + 1:]
    nxt = rest.find("\n## ")
    return rest[:nxt] if nxt != -1 else rest


def _prose_only(section: str) -> str:
    """Section text with the marker-kind enumeration removed."""
    out = []
    for line in section.splitlines():
        low = line.lower()
        if low.startswith("fires on ") or low.startswith("sweep`, `ae-and-sweep"):
            continue
        if "silent on" in low or low.strip() == "channel.":
            continue
        out.append(line)
    return "\n".join(out)


class TestLane2CarriesNoLane1Authority(unittest.TestCase):
    """AC2. Asserted rather than reviewed, because review already failed once."""

    def test_no_authority_grant_in_the_lane_2_section(self):
        prose = _norm(_prose_only(_section("Lane 2")))
        for phrase in _AUTHORITY:
            self.assertNotIn(phrase, prose, f"Lane 2 must not be granted: {phrase!r}")

    def test_the_lane_2_section_states_its_prohibitions_positively(self):
        """Absence of a grant is not the same as a stated boundary. The
        Documents file had no explicit prohibition either, which is part of how
        the wrong text went unnoticed."""
        prose = _norm(_section("Lane 2"))
        for required in ("does not push", "merge", "close", "file issues"):
            self.assertIn(required, prose)
        self.assertIn("r-0157", prose)

    def test_merge_and_close_is_granted_to_lane_1_only(self):
        """The authority exists; it belongs in exactly one section."""
        self.assertIn("merge and close directly", _norm(_section("Lane 1")))
        self.assertNotIn("merge and close directly", _norm(_prose_only(_section("Lane 2"))))
        self.assertNotIn("merge and close directly", _norm(_prose_only(_section("Lane 3"))))


class TestEveryRoleHasTheOwnOutputPredicate(unittest.TestCase):
    """AC10 — the check orthogonal to the other two, per role."""

    def test_the_shared_section_states_it(self):
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("never answered", text)
        self.assertIn("my own posted markers", text)

    def test_it_is_named_as_the_orthogonal_one(self):
        """Stating the check is not enough — the reason it exists is what stops
        it being dropped as redundant with the other two."""
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("orthogonal", text)
        self.assertIn("hrse#1715", text)


class TestRefusalIsStatedBeforeArming(unittest.TestCase):
    """The settled decision: no lane means no protocols, neither one."""

    def test_no_lane_fallback_to_lane_1(self):
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("do not fall back to lane 1", text)

    def test_identity_is_asserted_before_polling(self):
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("assert_identity", text)
        self.assertIn("refuse rather than return empty", text)


class TestNoBareGh(unittest.TestCase):
    """AC4/TC4 — every documented invocation is wrapped."""

    def test_every_gh_invocation_in_the_skill_is_scoped(self):
        for i, line in enumerate(SKILL.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if re.search(r"(?<![-\w])gh\s+(api|search|issue|pr|repo)\b", stripped):
                self.assertIn("gh-as", stripped, f"line {i}: bare gh — {stripped!r}")

    def test_gh_auth_switch_appears_nowhere(self):
        """R-0014. It mutates global state for every other session on the box."""
        self.assertNotIn("gh auth switch", SKILL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
