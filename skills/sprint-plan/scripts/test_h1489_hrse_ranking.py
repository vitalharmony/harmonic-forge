"""hrse#1489 — the amended ordering, the stage-map extension, and the ratchet.

The named-issue tests below use **real body text**, abbreviated but not
paraphrased, because the whole failure this issue corrects was a synthetic
fixture that passed while the live population stayed unclassified: hrse#1476's
Episodes test pinned a body containing `` `:Episode` `` that none of the four
real issues actually have.
"""
import unittest

import forge_pipeline_triage as fpt
import pipeline_rank as pr


class AmendedOrderingTests(unittest.TestCase):
    """TC-1 / TC-2 — the clock check runs before the stage-less bail."""

    def test_stage_less_issue_with_the_label_reaches_tier_2(self):
        # hrse#197 is the live case: it names no code surface at all, so
        # under the original ordering it returned UNCLASSIFIED before the
        # label was ever consulted.
        got = pr.rank("Sunset RapidAPI LinkedIn post sync",
                      "The vendor is shutting this down.", {pr.CLOCK_LABEL})
        self.assertEqual(got.group, "2")
        self.assertIn(pr.CLOCK_LABEL, got.why)

    def test_stage_less_keyword_path_is_independent_of_the_label(self):
        """Asserted separately from the label so neither can carry the other."""
        got = pr.rank("Sunset RapidAPI LinkedIn post sync",
                      "Vendor sunset; the endpoint goes away.", set())
        self.assertEqual(got.group, "2")
        self.assertIn("external clock", got.why)

    def test_stage_less_domain_vocabulary_is_not_an_external_clock(self):
        """TC-2. The five measured false positives, by their real text.

        Promoting `CLOCK` to the first substantive step re-armed constraint
        (a)'s failure class — an English or domain word colliding with the
        lexicon — inside the clock lexicon itself.
        """
        for title, body in (
            ("Trust Loop ledger", "Lapsed — due date long past; quietly expired."),
            ("HITL delivery", "72 hours (inherits bridge TTL) ... TTL expiry"),
            ("Trust Bridges", "SET key value EX ttl, not GET + EXPIRE"),
            ("Pulse Hint", "time-to-live expiry on the hint"),
            ("Mobile epic", "Refresh token rotation ON for the app"),
        ):
            with self.subTest(title=title):
                self.assertNotEqual(pr.rank(title, body, set()).group, "2")

    def test_a_staged_issue_still_uses_the_full_lexicon(self):
        """The narrowing applies to the stage-less path only — an issue that
        has already demonstrated it is about the pipeline keeps the looser
        terms, where `expiry` usually IS an external clock (OAuth tokens,
        certificates). Nine of fifteen staged tier-2 issues rest on that term
        alone, so widening the narrowing would be a far larger change than
        this issue sanctioned."""
        got = pr.rank("Google token handling",
                      "google_auth_service refresh token expiry handling", set())
        self.assertEqual(got.group, "2")


class StageMapExtensionTests(unittest.TestCase):
    """TC-3 — the four named issues rank non-UNCLASSIFIED."""

    CASES = {
        1437: ("Episodes: composer — add a narrated episode, saved unparsed",
               "Save creates `SourceRecord` + `Episode` + `INVOLVES` immediately, "
               "unparsed. `occurred_at` date picker defaulting to today."),
        1439: ("Episodes: evolved pills — What was found tab, edge type on the pill",
               "Replaces `DictationStagingPanel.tsx` (342 lines) entirely. Edge type "
               "is a dropdown populated from the backend `RelationshipType` enum. "
               "Evidence line under every relationship pill — the `source_quote`."),
        1441: ("Episodes: multi-person — N INVOLVES, group pill, per-person note",
               "One `Episode` object, N `INVOLVES` edges to `Person` — never N "
               "copies. An optional annotation riding on the `INVOLVES` edge."),
        1443: ("Episodes: the one delete button — archive/retract/erase",
               "The dialog must query the live lineage index and cite the receipt "
               "— not derive current state from `Commitment` alone."),
    }

    def test_named_episodes_issues_are_not_unclassified(self):
        for number, (title, body) in self.CASES.items():
            with self.subTest(issue=number):
                got = pr.rank(title, body, {"feature", "ui"})
                self.assertNotEqual(
                    got.group, pr.GROUP_UNCLASSIFIED,
                    f"hrse#{number} still ranks UNCLASSIFIED: {got.why}")

    def test_lowercase_english_does_not_match_the_label_patterns(self):
        """The `(?-i:...)` guard is load-bearing, not tidiness.

        Case-insensitively, `\\bINVOLVES\\b` matches the ordinary English verb
        — the same collision as `\\bBLOCKS\\b` matching "Blocks #1477", which
        is why the map bans bare capitalized English words.
        """
        got = pr.rank("Some process",
                      "This work involves a commitment from the team to an episode "
                      "of planning.", set())
        self.assertEqual(got.group, pr.GROUP_UNCLASSIFIED, got.why)


class RatchetTests(unittest.TestCase):
    """TC-4 — fails when the rate worsens, and can actually fire."""

    def _rankings(self, unclassified: int, total: int):
        return ([pr.Ranking(pr.GROUP_UNCLASSIFIED, "x")] * unclassified
                + [pr.Ranking("1", "y")] * (total - unclassified))

    def test_passes_at_the_recorded_baseline(self):
        ok, message = pr.check_ratchet(self._rankings(7, 44))
        self.assertTrue(ok, message)

    def test_fails_when_worse_than_the_baseline(self):
        ok, message = pr.check_ratchet(self._rankings(13, 45))
        self.assertFalse(ok)
        self.assertIn("FAILED", message)
        self.assertIn("do not raise the baseline", message)

    def test_empty_population_is_not_a_failure(self):
        ok, _ = pr.check_ratchet([])
        self.assertTrue(ok)


class ClassifyBothReposTests(unittest.TestCase):
    HRSE = [{"number": 1437, "title": "Episodes: composer",
             "body": "Save creates `SourceRecord` + `Episode` + `INVOLVES`.",
             "labels": [{"name": "feature"}, {"name": "ui"}]}]
    FORGE_META = {96: {"theme": "Ops", "venture": "CymaGraph", "title": "OAuth scopes"}}

    def test_hrse_issues_are_emitted_as_rows(self):
        """Finding 1's mechanism: `classify()` used to emit forge rows only."""
        entries = fpt.classify({96}, self.FORGE_META, self.HRSE)
        hrse_rows = [e for e in entries if e.repo == "hrse"]
        self.assertEqual([e.issue for e in hrse_rows], [1437])
        self.assertNotEqual(hrse_rows[0].group, pr.GROUP_UNCLASSIFIED)

    def test_forge_rows_keep_their_bucket(self):
        entries = fpt.classify({96}, self.FORGE_META, self.HRSE)
        forge_rows = [e for e in entries if e.repo == "forge"]
        self.assertEqual(len(forge_rows), 1)
        self.assertIn(forge_rows[0].bucket, {"A", "B", "C"})

    def test_reference_scan_is_repo_wide_not_milestone_filtered(self):
        """The regression the CLI diff caught: filtering the scan to the
        current milestone dropped every citation and collapsed every forge
        row to bucket C, because the other open hrse bodies stopped being
        read."""
        in_milestone = [{"number": 1, "title": "a", "body": "no refs",
                         "labels": []}]
        repo_wide = in_milestone + [
            {"number": 999, "title": "b", "body": "This is blocked on forge#96.",
             "labels": []}]
        entries = fpt.classify({96}, self.FORGE_META, in_milestone,
                               hrse_reference_sources=repo_wide)
        forge_row = next(e for e in entries if e.repo == "forge")
        self.assertEqual(forge_row.bucket, "A")
        self.assertEqual(forge_row.referenced_by[0]["issue"], 999)

    def test_reverse_scan_records_forge_citing_hrse(self):
        """hrse#1476's rescope promised this direction and did not ship it."""
        forge_issues = [{"number": 96, "title": "OAuth scopes",
                         "body": "Needed for hrse#1437.", "labels": []}]
        entries = fpt.classify({96}, self.FORGE_META, self.HRSE, forge_issues)
        hrse_row = next(e for e in entries if e.repo == "hrse")
        self.assertEqual(hrse_row.referenced_by[0]["repo"], "forge")
        self.assertEqual(hrse_row.referenced_by[0]["issue"], 96)

    def test_render_uses_group_headings_and_no_bucket_headings(self):
        """TC-7's shape, asserted on the pure renderer.

        hrse#1522: renamed from "...tier_headings..." along with the
        assertion below — "Tier" is now the board's model-routing field
        name, and `render()`'s headings say "Group" so the two are never
        rendered under one indistinguishable label.
        """
        out = fpt.render(fpt.classify({96}, self.FORGE_META, self.HRSE))
        self.assertIn("Group", out)
        self.assertNotIn("Bucket A —", out)
        self.assertIn("hrse#1437", out)
        self.assertIn("UNCLASSIFIED", out)  # the ratchet line


class CacheTests(unittest.TestCase):
    """TC-8 / NC6 — AC5 is the board read, and the fallback is covered."""

    def test_board_ttl_is_not_zero(self):
        """`ttl<=0` means "always fetch"; the previous value never touched
        the cache at all."""
        self.assertGreater(fpt.BOARD_TTL_SECONDS, 0)
        self.assertEqual(fpt.BOARD_TTL_SECONDS, 1800)

    def test_the_no_cache_fallback_is_reachable_and_tested(self):
        """`fetch_forge_theme_venture` has a `_item_list_cache is None`
        branch that bypasses caching entirely. A test that only injects
        `cache_dir=` exercises the import-succeeded path and says nothing
        about this one."""
        saved = fpt._item_list_cache
        calls = []
        try:
            fpt._item_list_cache = None
            original = fpt._run
            fpt._run = lambda *a: calls.append(a) or '{"items": []}'
            try:
                self.assertEqual(fpt.fetch_forge_theme_venture(), {})
            finally:
                fpt._run = original
        finally:
            fpt._item_list_cache = saved
        self.assertTrue(calls, "the fallback never ran")
        self.assertIn("item-list", calls[0])


if __name__ == "__main__":
    unittest.main()
