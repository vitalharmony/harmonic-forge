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


class TestRunnableBeltCommandPerLane(unittest.TestCase):
    """harmonic-forge#570 AC1/AC2/AC7 -- a literal, runnable, unambiguous-path
    command per lane, not a description of one."""

    def test_every_path_reference_is_unambiguous(self):
        """A bare `tools/gh/...` reads as skill-relative from this file's
        real location two directories below the repo root, and resolves to
        nothing there -- every mention must be absolute or explicitly
        repo-root-relative, INCLUDING inside fenced code blocks (a runnable
        command is exactly where AC2's own regression would land -- a
        backtick-only scan misses every command in a ``` fence)."""
        text = SKILL.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            idx = line.find("tools/gh/")
            while idx != -1:
                prefix = line[:idx]
                if not (prefix.endswith("~/harmonic-forge/") or prefix.endswith("harmonic-forge/")):
                    self.fail(f"line {i}: ambiguous 'tools/gh/' reference: {line!r}")
                idx = line.find("tools/gh/", idx + 1)

    def test_lane_1_belt_is_worktrees_first_not_a_repo_wide_sweep(self):
        """harmonic-forge#590 inverted this: the repo-wide `--queue-for l1`
        sweep is the SUSPENDERS' backstop, and arming it as Lane 1's belt was
        the regression. This test used to assert the opposite -- it has been
        red since #590 merged, which is exactly how a vacuous guard in the
        sibling suite survived review (#594 preclose finding)."""
        text = SKILL.read_text(encoding="utf-8")
        lane1 = text.split("- **Lane 1**", 1)[1].split("- **Lane 2**", 1)[0]
        self.assertIn("--all-worktrees", lane1)
        self.assertNotIn("--queue-for l1", lane1)

    #: The block harmonic-forge#607 rewrote. Scoped, not a whole-file search:
    #: the first version of these guards passed with two of the three filter
    #: rows deleted, because `QUEUE_KINDS` and `discussion` already appeared
    #: elsewhere in the file (#607 preclose finding).
    def _filter_block(self) -> str:
        text = SKILL.read_text(encoding="utf-8")
        start = text.index("**The lane belts are a different mechanism")
        end = text.index("**This rule is only total if Lane 1 posts")
        return text[start:end]

    def test_the_filter_block_names_every_filter_the_code_applies(self):
        """Doc-SYNC, not doc-only: imports the module and compares. Renaming
        or deleting `_is_l2_finding` in the code must fail this, which the
        first version of this guard did not do -- it never read the code, so
        the AC's "while `_is_l2_finding` exists" condition was unevaluated."""
        sys.path.insert(0, str(SKILL.parent.parent.parent / "tools" / "gh"))
        sys.path.insert(0, str(SKILL.parent.parent.parent / "tools" / "onboard"))
        import watch_lane_posts
        self.assertTrue(hasattr(watch_lane_posts, "_is_l2_finding"),
                        "the doc documents a filter the code no longer has")
        for lane, kinds in watch_lane_posts.QUEUE_KINDS.items():
            if "discussion" in kinds:
                self.fail("`discussion` is queue-eligible again; the doc says "
                          "it was removed and measured")
        # Assert the TABLE ROWS, not the tokens. #607's first guard checked
        # whether each name appeared anywhere in the block -- and every one of
        # them also appears in the block's own prose, so deleting two of the
        # three rows shipped green. Measured, before this fix:
        #   delete `QUEUE_KINDS[lane]` row  -> 0 failures
        #   delete `_is_l2_finding` row     -> 0 failures
        rows = [line for line in self._filter_block().splitlines()
                if line.startswith("| `")]
        self.assertEqual(len(rows), 3, f"expected three filter rows, got {rows!r}")
        subjects = [line.split("`")[1] for line in rows]
        self.assertEqual(subjects, ["QUEUE_KINDS[lane]", "_is_l2_finding", "discussion"],
                         f"a filter row was removed or reordered: {subjects!r}")

    def test_the_block_does_not_claim_the_belts_have_no_filters(self):
        """harmonic-forge#607. The retired claim, scoped to the block that
        describes the belts -- the identical sentence is CORRECT of
        `discover_l1_sweep` and stays there."""
        for claim in ("no precedence table", "no exclusion list"):
            self.assertNotIn(claim, self._filter_block())

    def test_the_sweep_keeps_the_claim_that_is_true_of_it(self):
        """`discover_l1_sweep` really does filter nothing -- executed:
        `[handoff, L2 Finding]` puts the issue IN the sweep. Deleting this
        sentence would lose a true statement, which #607's first attempt did."""
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("no precedence table, no exclusion list", text)
        self.assertIn("discover_l1_sweep", text)

    def test_each_filter_row_cites_its_incident(self):
        """A filter with no recorded reason reads as accretion, and this repo
        deletes accretion. Scoped so deleting any one row fails."""
        block = self._filter_block()
        self.assertIn("580", block, "the finding exclusion must cite #580")
        self.assertIn("63 issues", block, "the discussion removal must cite its measurement")

    def test_the_repo_wide_sweep_survives_in_the_suspenders(self):
        """Demoted, not deleted -- and armed there as a literal command."""
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("--queue-for l1", text.split("## The suspenders", 1)[1])

    def test_lane_2_belt_is_worktrees_first(self):
        """harmonic-forge#596: naming the shared lane-2 checkout resolved 0/1 in
        the detached-HEAD resting state, and Lane 2 is required to work in
        `/tmp/<repo>-<issue>-impl` anyway -- the one path a static list can
        name is the one path it may not work in."""
        text = SKILL.read_text(encoding="utf-8")
        lane2 = text.split("- **Lane 2**", 1)[1].split("- **Lane 3**", 1)[0]
        self.assertIn("--all-worktrees", lane2)
        self.assertNotIn("--worktrees ~/", lane2)
        # And queue discovery, which worktrees-only silently removed: Lane 2's
        # inbound handoff has no worktree by construction (#596 preclose).
        self.assertIn("--queue-for l2", lane2)

    def test_lane_3_belt_derives_its_repo_set(self):
        """harmonic-forge#596: a belt scanning a subset is a partial belt and
        looks exactly like a whole one -- and per R-0122 the set is derived
        from the manifest, not listed here."""
        text = SKILL.read_text(encoding="utf-8")
        lane3 = text.split("- **Lane 3**", 1)[1].split("**A monitor that never", 1)[0]
        self.assertIn("--account-repos", lane3)
        self.assertNotIn("--repo vitalharmony/", lane3)

    def test_states_the_belt_is_watch_lane_posts_and_rebuilding_is_the_defect(self):
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("`watch_lane_posts.py` already is the belt", text)
        self.assertIn("that re-derivation is the defect", text)

    def test_names_cron_and_hand_written_poller_as_neither_mechanism(self):
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("`croncreate` and a hand-written poller script are neither", text)

    def test_states_the_zero_resolved_reporting_guarantee(self):
        text = _norm(SKILL.read_text(encoding="utf-8"))
        self.assertIn("never silently, when zero resolved", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
