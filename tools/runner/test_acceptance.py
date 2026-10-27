"""harmonic-forge#1112 AC11: one acceptance-heading matcher, shared by
`acceptance.acceptance_criteria` and `te_run.ac_titles`."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance  # noqa: E402
import sandbox  # noqa: E402
import te_run  # noqa: E402

#: F1073's shape: `## Acceptance` and checkbox items.
F1073_BODY = ("## Problem\n\nSomething.\n\n## Acceptance\n\n"
              "- [ ] The emitter writes one line per event.\n"
              "- [ ] A missing field is refused.\n\n## Notes\n\nnot a criterion\n")


class Shared(unittest.TestCase):
    def test_f1073_shaped_body_is_read_by_acceptance_criteria_and_ac_titles(self):
        with mock.patch.object(acceptance, "issue_body", return_value=F1073_BODY):
            criteria = acceptance.acceptance_criteria("vitalharmony/harmonic-forge", 1073, {})
        self.assertIn("one line per event", criteria)
        self.assertNotIn("not a criterion", criteria)
        self.assertEqual(te_run.ac_titles(F1073_BODY),
                         ["The emitter writes one line per event.", "A missing field is refused."])

    def test_heading_variants(self):
        for heading in ("## Acceptance criteria", "## acceptance", "### Acceptance Criteria (amended)",
                        "## ACCEPTANCE criteria:"):
            body = f"{heading}\n\n1. One.\n\n## Next\n\nx\n"
            self.assertEqual(acceptance.items(acceptance.section(body)), ["One."], heading)

    def test_section_ends_at_same_or_higher_level_only(self):
        body = "## Acceptance\n\n1. One.\n\n### Detail\n\n2. Two.\n\n## Next\n\n3. Three.\n"
        self.assertEqual(acceptance.items(acceptance.section(body)), ["One.", "Two."])

    def test_no_section(self):
        self.assertIsNone(acceptance.section("## Acceptable risk\n\n1. x\n"))
        with mock.patch.object(acceptance, "issue_body", return_value="## Problem\n\nx\n"):
            with self.assertRaises(sandbox.RunnerEnvError):
                acceptance.acceptance_criteria("vitalharmony/harmonic-forge", 1, {})


class Anchored(unittest.TestCase):
    """harmonic-forge#1112 sticky-wicket patch 1: the heading is anchored and
    fenced code is skipped; a section with no items is refused."""

    def test_f1112_own_body_reads_its_criteria(self):
        # F1112's live body has a wrapped table cell starting `## Acceptance
        # criteria\s*$`; ...` above its real section.
        body = (Path(__file__).resolve().parent / "testdata" / "f1112_body.md").read_text()
        found = acceptance.items(acceptance.section(body))
        self.assertGreaterEqual(len(found), 15)
        self.assertTrue(found[0].startswith("Every `mise` run inside a boundary"), found[:1])
        with mock.patch.object(acceptance, "issue_body", return_value=body):
            criteria = acceptance.acceptance_criteria("vitalharmony/harmonic-forge", 1112, {})
        self.assertIn("share one heading matcher", criteria)
        self.assertGreaterEqual(len(te_run.ac_titles(body)), 15)

    def test_heading_inside_fenced_code_is_skipped(self):
        body = ("## Problem\n\n```\n## Acceptance criteria\n\n1. Fake.\n```\n\n"
                "## Acceptance criteria\n\n1. Real.\n\n## Next\n")
        self.assertEqual(acceptance.items(acceptance.section(body)), ["Real."])

    def test_acceptance_testing_notes_is_not_the_section(self):
        body = ("## Acceptance testing notes\n\n1. Not a criterion.\n\n"
                "## Acceptance criteria\n\n1. Real.\n")
        self.assertEqual(acceptance.items(acceptance.section(body)), ["Real."])

    def test_section_with_no_items_is_refused_before_spend(self):
        with mock.patch.object(acceptance, "issue_body",
                               return_value="## Acceptance criteria\n\nprose only\n"):
            with self.assertRaises(sandbox.RunnerEnvError):
                acceptance.acceptance_criteria("vitalharmony/harmonic-forge", 1, {})


class FenceMatching(unittest.TestCase):
    """CommonMark: a fence closes only on the same character, at least as long."""

    REAL = "## Acceptance\n\n1. Real.\n"

    def test_mismatched_character_does_not_close(self):
        body = "```\n~~~\n## Acceptance\n\n1. Fake.\n```\n" + self.REAL
        self.assertEqual(acceptance.items(acceptance.section(body)), ["Real."])

    def test_shorter_close_does_not_close(self):
        body = "````\n```\n## Acceptance\n\n1. Fake.\n````\n" + self.REAL
        self.assertEqual(acceptance.items(acceptance.section(body)), ["Real."])

    def test_longer_close_closes(self):
        body = "~~~\ncode\n~~~~~\n" + self.REAL
        self.assertEqual(acceptance.items(acceptance.section(body)), ["Real."])


if __name__ == "__main__":
    unittest.main()
