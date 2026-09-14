#!/usr/bin/env python3
"""harmonic-forge#651 AC8: assert SKILL.md and watch_lane_posts.py's own
module docstring both match CANONICAL_BELTS -- byte-for-byte, as PARSED
argparse values, never a hand-typed comparison that could itself drift.

Two documents describe the same table (`watch_lane_posts.CANONICAL_BELTS`):
`skills/belt-and-suspenders/SKILL.md` (the operator-facing arming
instructions) and `watch_lane_posts.py`'s own module docstring (the
`Usage` block anyone reading the script sees first). AC1's whole point is
that there is now exactly ONE canonical command per lane/mode; a doc that
silently drifts from the table -- or from its sibling doc -- reintroduces
the 2026-09-14 incident's proximate cause one level up: a session that
copies a stale example from whichever of the two docs it happened to read.

This is a doc-sync check, not a runtime-behavior test: it fails loudly the
moment anyone hand-edits one of the two docs (or the table) without the
others, which is exactly the property `tools/gh/test_watch_lane_posts.py`'s
own `BeltSkillDocSyncTests` establishes for a different pair of facts.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_lane_posts  # noqa: E402

_SKILL_MD = (Path(__file__).resolve().parent.parent.parent
             / "skills" / "belt-and-suspenders" / "SKILL.md")

# Both docs' example commands are written as `python3 <script> <flags...>`,
# optionally split across lines with a trailing `\` continuation (the
# module docstring's style) -- never as a single physical line in either
# doc, so the continuation is joined away BEFORE matching, rather than
# handled inside the regex itself: `[^\n]` happily consumes a bare `\`
# (it is not a newline), which starves a `\\\n` alternative positioned
# after it of the backslash it needs -- the joined-first approach sidesteps
# that class of regex bug entirely instead of tuning around it.
_COMMAND_RE = re.compile(
    r"python3\s+(?:watch_lane_posts\.py|~/harmonic-forge/tools/gh/watch_lane_posts\.py)"
    r"([^\n`]*)"
)


def _extract_commands(text: str) -> list[list[str]]:
    """Every `python3 .../watch_lane_posts.py <flags>` example in `text`,
    each tokenized into an argv list."""
    joined = text.replace("\\\n", " ")
    return [match.group(1).split() for match in _COMMAND_RE.finditer(joined)]


def _canonical_entries() -> list[tuple[str, list[str]]]:
    """Every `CANONICAL_BELTS` entry, flattened to `(label, argv)` in table
    order -- lane "1", "2", then lane "3"'s belt before its sweep, matching
    the order both docs present them in."""
    entries = []
    for lane in ("1", "2", "3"):
        for i, entry in enumerate(watch_lane_posts.CANONICAL_BELTS[lane]):
            label = f"lane{lane}" if i == 0 else f"lane{lane}-sweep"
            entries.append((label, entry["argv"]))
    return entries


def _normalize(parser, argv: list[str]) -> dict:
    ns = parser.parse_args(argv)
    d = vars(ns).copy()
    d["watch"] = sorted(d.get("watch") or [])
    return d


class BeltSkillMatchesCanonicalTableTests(unittest.TestCase):

    def setUp(self):
        self.parser = watch_lane_posts._build_parser()
        self.canonical = _canonical_entries()

    def test_skill_md_has_exactly_the_canonical_commands(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        extracted = _extract_commands(text)
        self.assertEqual(
            len(extracted), len(self.canonical),
            f"SKILL.md must show exactly {len(self.canonical)} belt/sweep "
            f"commands (one per CANONICAL_BELTS entry); found {len(extracted)}: "
            f"{extracted}")
        for (label, canonical_argv), argv in zip(self.canonical, extracted):
            with self.subTest(entry=label):
                self.assertEqual(
                    _normalize(self.parser, argv),
                    _normalize(self.parser, canonical_argv),
                    f"SKILL.md's {label} command does not match "
                    f"CANONICAL_BELTS[...] once parsed")

    def test_module_docstring_has_exactly_the_canonical_commands(self):
        text = watch_lane_posts.__doc__ or ""
        extracted = _extract_commands(text)
        self.assertEqual(
            len(extracted), len(self.canonical),
            f"watch_lane_posts.py's module docstring must show exactly "
            f"{len(self.canonical)} belt/sweep commands; found "
            f"{len(extracted)}: {extracted}")
        for (label, canonical_argv), argv in zip(self.canonical, extracted):
            with self.subTest(entry=label):
                self.assertEqual(
                    _normalize(self.parser, argv),
                    _normalize(self.parser, canonical_argv),
                    f"watch_lane_posts.py docstring's {label} command does "
                    f"not match CANONICAL_BELTS[...] once parsed")

    def test_skill_md_and_docstring_examples_agree_with_each_other(self):
        """Not just each vs. the table -- each vs. the OTHER doc, so a typo
        that happens to also land in CANONICAL_BELTS (impossible, since the
        table is code, but the point of this test is not to rely on that)
        cannot hide two docs quietly disagreeing with each other."""
        skill_extracted = _extract_commands(_SKILL_MD.read_text(encoding="utf-8"))
        doc_extracted = _extract_commands(watch_lane_posts.__doc__ or "")
        self.assertEqual(len(skill_extracted), len(doc_extracted))
        for skill_argv, doc_argv in zip(skill_extracted, doc_extracted):
            self.assertEqual(
                _normalize(self.parser, skill_argv),
                _normalize(self.parser, doc_argv))

    def test_no_retired_sweep_for_l1_example_remains_in_either_doc(self):
        """--sweep-for l1 is fully retired (harmonic-forge#640) -- neither
        doc may show it as a runnable example, prose retirement notes
        excepted (those don't match _COMMAND_RE, which requires the
        script-invocation prefix)."""
        for path_or_text, name in (
            (_SKILL_MD.read_text(encoding="utf-8"), "SKILL.md"),
            (watch_lane_posts.__doc__ or "", "watch_lane_posts.py docstring"),
        ):
            for argv in _extract_commands(path_or_text):
                with self.subTest(doc=name, argv=argv):
                    self.assertFalse(
                        "--sweep-for" in argv and "l1" in argv,
                        f"{name} still shows a runnable --sweep-for l1 example")


if __name__ == "__main__":
    unittest.main()
