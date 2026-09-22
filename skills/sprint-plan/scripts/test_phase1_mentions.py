"""Extraction-layer tests for hrse#917 phase 1 (`phase1_mentions.py`)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from phase1_mentions import HRSE_REPO, FORGE_REPO, extract_mentions


class RangeExpansionTests(unittest.TestCase):
    def test_en_dash_plus_hash_range_expands_fully(self):
        """The doc's actual form is `#794–#797` (en dash *plus* a repeated
        `#`), not `#794-797` — today's code silently drops 795/796."""
        text = "See #794–#797 for the remainder."
        mentions = extract_mentions(text)
        issues = sorted(m.issue for m in mentions if m.repo == HRSE_REPO)
        self.assertEqual(issues, [794, 795, 796, 797])

    def test_plain_hyphen_range_still_expands(self):
        text = "See #10-12 for detail."
        mentions = extract_mentions(text)
        self.assertEqual(sorted(m.issue for m in mentions), [10, 11, 12])

    def test_forge_slash_range_expands(self):
        text = "See harmonic-forge#5/#7 for detail."
        mentions = extract_mentions(text)
        self.assertEqual(
            sorted(m.issue for m in mentions if m.repo == FORGE_REPO), [5, 7]
        )


class BoardLinkTests(unittest.TestCase):
    def test_board_links_are_excluded_from_repo_numbers_and_flagged(self):
        text = (
            "the boards ([hrse #1](https://github.com/users/vitalharmony/projects/1), "
            "[forge #3](https://github.com/users/vitalharmony/projects/3))"
        )
        mentions = extract_mentions(text)
        self.assertEqual(len(mentions), 2)
        self.assertTrue(all(m.is_board_link for m in mentions))
        self.assertEqual({(m.repo, m.issue) for m in mentions},
                          {(HRSE_REPO, 1), (FORGE_REPO, 3)})

    def test_board_link_number_is_not_double_counted_as_a_bare_mention(self):
        text = "[hrse #42](https://github.com/users/vitalharmony/projects/1)"
        mentions = extract_mentions(text)
        self.assertEqual(len(mentions), 1)
        self.assertTrue(mentions[0].is_board_link)


class RepoScopingTests(unittest.TestCase):
    def test_bare_number_is_hrse_forge_prefixed_is_forge(self):
        text = "#46 is unrelated to harmonic-forge#46."
        mentions = extract_mentions(text)
        by_repo = {(m.repo, m.issue) for m in mentions}
        self.assertIn((HRSE_REPO, 46), by_repo)
        self.assertIn((FORGE_REPO, 46), by_repo)
        # Not double-matched — exactly one mention per repo.
        self.assertEqual(len(mentions), 2)


class OccurrenceAndCoverageTests(unittest.TestCase):
    def test_occurrence_ordinal_increments_in_document_order(self):
        text = "First #100. Second, still #100. Third #100 too."
        mentions = extract_mentions(text)
        occurrences = [m.occurrence for m in mentions]
        self.assertEqual(occurrences, [1, 2, 3])

    def test_preamble_mention_before_first_bullet_is_extracted(self):
        text = "A preamble mentions #631 before any bullet.\n\n- #632 is a bullet."
        mentions = extract_mentions(text)
        self.assertIn(631, [m.issue for m in mentions])

    def test_h2_boundary_assigns_correct_heading(self):
        text = "## First\nMentions #1 here.\n## Second\nMentions #2 here.\n"
        mentions = extract_mentions(text)
        headings = {m.issue: m.heading for m in mentions}
        self.assertEqual(headings[1], "First")
        self.assertEqual(headings[2], "Second")

    def test_unit_kind_distinguishes_list_item_from_prose(self):
        text = "## H\nPreamble sentence mentions #1.\n- Bullet mentions #2.\n"
        mentions = extract_mentions(text)
        kinds = {m.issue: m.unit_kind for m in mentions}
        self.assertEqual(kinds[1], "prose")
        self.assertEqual(kinds[2], "list-item")


if __name__ == "__main__":
    unittest.main()
