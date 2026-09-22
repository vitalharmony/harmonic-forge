"""Composite-fixture, elimination-gate, and coverage tests for hrse#917
phase 1 (`phase1_report.py`). No network, no `gh api`, no graph, no data
mutation — every gate here is checked against a synthetic document that
embeds the real doc's own #848/#860 wording verbatim (per the Implementation
Spec's "Ground truth, verified live by Lane 1").
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from phase1_candidates import CANDIDATE_IDS
from phase1_mentions import extract_mentions
from phase1_report import (
    build_adjudication_rows,
    build_rows,
    coverage_summary,
    elimination_report,
    legacy_scan_start,
    mention_signature,
)

# Verbatim from docs/PRIORITIES.md, cited in the Implementation Spec.
_H848_CANONICAL = (
    "**hrse#848 — relationship-health index integrity. Closed 2026-08-13**, "
    "merged hrse#851. Operator had promoted it the same day.\n"
)
_H860_ALL_THREE = (
    "- **#860 remains open** (outstanding work from any merge/close gets a "
    "real tracker in the same action, never left as prose in a comment).\n"
    "- With those merged, the migration-control thread is complete except "
    "**#860**.\n"
    "- **#860** is the only one of this set still unpicked.\n"
)
_BARE_849_BESIDE_855_CLOSED = (
    "- See #855 closed. Also relevant: #849 needs another look.\n"
)
_HISTORICAL_CHAIN = (
    "- Rejected: it preserves two representations of one fact — the same "
    "divergence class the #849 → #859 → #861 → #866 → #867 chain was built "
    "to eliminate.\n"
)
_RANGE_AND_BOARD_LINKS = (
    "- hrse#793 → the rest of 2.7 — closed 2026-08-13. #794–#797 and #799 "
    "are now technically unblocked.\n"
    "- Everything mechanical: the boards "
    "([hrse #1](https://github.com/users/vitalharmony/projects/1), "
    "[forge #3](https://github.com/users/vitalharmony/projects/3)).\n"
)
_SPACER_BULLET = "- Nothing new here; purely a spacer bullet with no issue references.\n"

CLOSED_BY_REPO = {
    "vitalharmony/hrse": {848, 849, 851, 855, 859, 860, 861, 866, 867, 793},
    "vitalharmony/harmonic-forge": set(),
}


def _fixture(spacer_at: str | None = None) -> str:
    """The composite fixture. `spacer_at` inserts the mention-free,
    status-free spacer bullet at one of three adversarial positions."""
    preamble = "A preamble mentions #631 for coverage testing.\n\n"
    heading = "## In flight\n\n"
    body_parts = [_H848_CANONICAL, "\n"]
    if spacer_at == "before-first-bullet":
        body_parts.append(_SPACER_BULLET)
    body_parts.append(_H860_ALL_THREE)
    body_parts.append(_BARE_849_BESIDE_855_CLOSED)
    if spacer_at == "between-mention-and-status":
        body_parts.append(_SPACER_BULLET)
    body_parts.append(_HISTORICAL_CHAIN)
    if spacer_at == "before-heading":
        body_parts.append(_SPACER_BULLET)
    settled = "## Settled\n\n" + _RANGE_AND_BOARD_LINKS
    return preamble + heading + "".join(body_parts) + settled


def _canary_ids(mentions):
    must_flag = {m.mention_id for m in mentions if m.repo == "vitalharmony/hrse" and m.issue == 860}
    # #849's bare occurrence is "claim" role; its chain-citation occurrence
    # is "citation" — role classification is what separates them here.
    must_flag |= {
        m.mention_id for m in mentions
        if m.repo == "vitalharmony/hrse" and m.issue == 849 and m.role == "claim"
    }
    must_clear = {
        m.mention_id for m in mentions
        if m.repo == "vitalharmony/hrse" and m.issue == 848 and m.occurrence == 1
    }
    return must_flag, must_clear


class EliminationGateTests(unittest.TestCase):
    def setUp(self):
        self.text = _fixture()
        self.mentions = extract_mentions(self.text)
        self.matrix, self.coverage = build_rows(self.text, self.mentions, CLOSED_BY_REPO)
        self.must_flag, self.must_clear = _canary_ids(self.mentions)

    def test_canary_selection_found_the_intended_mentions(self):
        # Sanity check on the fixture itself, not the candidates: exactly
        # the 3 #860 occurrences plus the one bare (claim-role) #849.
        self.assertEqual(len(self.must_flag), 4)
        self.assertTrue(any(":849:" in mid for mid in self.must_flag))
        self.assertEqual(len(self.must_clear), 1)

    def test_c5_and_c6_pass_every_gate(self):
        report = elimination_report(self.matrix, self.must_flag, self.must_clear, self.coverage)
        self.assertTrue(report["C5"]["passed"], report["C5"]["failures"])
        self.assertTrue(report["C6"]["passed"], report["C6"]["failures"])

    def test_c1_c2_c3_c4_each_fail_at_least_one_gate(self):
        """Controls, including the negative control — expected to fail,
        demonstrating the exact failure modes this issue exists to catch."""
        report = elimination_report(self.matrix, self.must_flag, self.must_clear, self.coverage)
        for cid in ("C1", "C2", "C3", "C4"):
            self.assertFalse(report[cid]["passed"], f"{cid} unexpectedly passed every gate")

    def test_whole_h2_coverage_is_complete(self):
        c = coverage_summary(self.text, "whole-h2")
        self.assertEqual(c["scanned_chars"], c["total_chars"])
        self.assertEqual(c["unscanned_ranges"], [])

    def test_legacy_coverage_reports_the_omitted_range_explicitly(self):
        c = coverage_summary(self.text, "legacy")
        start = legacy_scan_start(self.text)
        self.assertEqual(c["unscanned_ranges"], [(0, start)])
        self.assertLess(c["scanned_chars"], c["total_chars"])

    def test_preamble_mention_is_unscanned_only_under_legacy(self):
        rows = [r for r in self.matrix if r["issue"] == 631 and r["repo"] == "vitalharmony/hrse"]
        legacy_rows = [r for r in rows if r["coverage"] == "legacy"]
        whole_rows = [r for r in rows if r["coverage"] == "whole-h2"]
        self.assertTrue(all(r["reason_code"] == "unscanned" for r in legacy_rows))
        self.assertTrue(all(r["reason_code"] != "unscanned" for r in whole_rows))

    def test_board_links_are_excluded_rows_not_omissions(self):
        rows = [r for r in self.matrix if r["reason_code"] == "board-link"]
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(r["outcome"] == "EXCLUDE" for r in rows))

    def test_bare_849_flags_under_c5_and_c6_despite_neighbouring_855_closed(self):
        bare_849 = next(
            m for m in self.mentions
            if m.repo == "vitalharmony/hrse" and m.issue == 849 and m.role == "claim"
        )
        for cid in ("C5", "C6"):
            row = next(
                r for r in self.matrix
                if r["coverage"] == "whole-h2" and r["rule"] == cid
                and r["mention_id"] == bare_849.mention_id
            )
            self.assertEqual(row["outcome"], "FLAG", f"{cid} wrongly cleared the bare #849")

    def test_c848_canonical_clears_under_c5_c6_and_flags_under_c1(self):
        canonical = next(mid for mid in self.must_clear)
        outcomes = {
            row["rule"]: row["outcome"]
            for row in self.matrix
            if row["coverage"] == "whole-h2" and row["mention_id"] == canonical
        }
        self.assertEqual(outcomes["C5"], "CLEAR")
        self.assertEqual(outcomes["C6"], "CLEAR")
        self.assertEqual(outcomes["C1"], "FLAG")

    def test_c2_c3_c4_specifically_clear_a_genuine_860_occurrence(self):
        """The gate each is meant to fail — not just 'some gate, somewhere'."""
        for cid in ("C2", "C3", "C4"):
            cleared_860 = [
                r for r in self.matrix
                if r["coverage"] == "whole-h2" and r["rule"] == cid
                and r["repo"] == "vitalharmony/hrse" and r["issue"] == 860
                and r["outcome"] == "CLEAR"
            ]
            self.assertGreater(len(cleared_860), 0, f"{cid} was expected to clear a genuine #860 drift")

    def test_matrix_carries_no_adjudication_column(self):
        self.assertTrue(all("lane1_adjudication" not in row for row in self.matrix))

    def test_every_candidate_and_coverage_combination_is_present(self):
        combos = {(row["rule"], row["coverage"]) for row in self.matrix}
        expected = {(cid, mode) for cid in CANDIDATE_IDS for mode in ("legacy", "whole-h2")}
        self.assertEqual(combos, expected)


class AdjudicationArtifactTests(unittest.TestCase):
    def test_adjudication_is_deduplicated_by_mention_id_with_blank_column(self):
        text = _fixture()
        mentions = extract_mentions(text)
        matrix, _ = build_rows(text, mentions, CLOSED_BY_REPO)
        adjudication = build_adjudication_rows(matrix)
        ids = [row["mention_id"] for row in adjudication]
        self.assertEqual(len(ids), len(set(ids)), "adjudication.csv must be deduplicated")
        self.assertTrue(all(row["lane1_adjudication"] == "" for row in adjudication))

    def test_adjudication_is_far_smaller_than_the_full_matrix(self):
        text = _fixture()
        mentions = extract_mentions(text)
        matrix, _ = build_rows(text, mentions, CLOSED_BY_REPO)
        adjudication = build_adjudication_rows(matrix)
        self.assertLess(len(adjudication), len(matrix) / 4)


class BulletInsertionInvarianceTests(unittest.TestCase):
    """Gate (v): a mention-free, status-free bullet inserted anywhere must
    not change a selectable candidate's flagged-occurrence signatures."""

    def _signatures(self, text: str, candidate_id: str) -> set:
        mentions = extract_mentions(text)
        matrix, _ = build_rows(text, mentions, CLOSED_BY_REPO)
        return {
            mention_signature(row)
            for row in matrix
            if row["coverage"] == "whole-h2" and row["rule"] == candidate_id
        }

    def test_c5_and_c6_are_invariant_across_all_three_insertion_points(self):
        base_c5 = self._signatures(_fixture(), "C5")
        base_c6 = self._signatures(_fixture(), "C6")
        for position in ("before-first-bullet", "between-mention-and-status", "before-heading"):
            with self.subTest(position=position):
                variant = _fixture(spacer_at=position)
                self.assertEqual(self._signatures(variant, "C5"), base_c5)
                self.assertEqual(self._signatures(variant, "C6"), base_c6)

    def test_c4_structural_scoping_is_the_expected_negative_case(self):
        """Documents, not asserts as broken forever: C4's unit-based
        scoping is exactly today's positional-block bug extended to the
        whole document, so it is *expected* to vary under insertion. If
        this ever starts passing, that's real news for phase 2, not a
        test to silently delete."""
        base = self._signatures(_fixture(), "C4")
        varied = self._signatures(_fixture(spacer_at="before-first-bullet"), "C4")
        # No assertion of inequality — recorded as characterization, not a
        # required failure. C4's own gate-based FAIL above is what matters.
        del base, varied


class ProductionIsolationTests(unittest.TestCase):
    def test_run_phase1_report_leaves_the_document_byte_identical(self):
        import tempfile
        from phase1_report import run_phase1_report

        text = _fixture()
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = Path(tmp) / "PRIORITIES.md"
            doc_path.write_text(text, encoding="utf-8")
            before = doc_path.read_bytes()
            run_phase1_report(
                doc_path=doc_path, output_dir=Path(tmp) / "out", closed_by_repo=CLOSED_BY_REPO
            )
            after = doc_path.read_bytes()
        self.assertEqual(before, after)

    def test_phase1_never_writes_to_the_document(self):
        import inspect
        import phase1_report

        source = inspect.getsource(phase1_report)
        self.assertNotIn(".write_text(", source)
        self.assertNotIn("open(doc_path, \"w\"", source)

    def test_run_phase1_report_does_not_import_main_or_branches_ahead(self):
        import inspect
        import phase1_report

        source = inspect.getsource(phase1_report.run_phase1_report)
        self.assertNotIn("branches_ahead_of_main", source)
        # Only imports DOC_PATH/REPOS/closed_issues from drift_check —
        # never drift_check.main itself.
        self.assertNotIn("import main", source)
        self.assertNotIn("drift_check.main", source)
        self.assertNotIn("= main(", source)
        self.assertNotIn("sys.exit(main(", source)

    def test_cli_dispatch_never_reaches_main_when_phase1_report_is_requested(self):
        """The `--phase1-report` branch in drift_check.py's `__main__` block
        must return/exit before `main()` is called."""
        import inspect
        import drift_check

        module_source = inspect.getsource(drift_check)
        dispatch = module_source[module_source.index('if __name__ == "__main__"'):]
        phase1_branch = dispatch[dispatch.index("--phase1-report"):dispatch.index("sys.exit(main())")]
        self.assertIn("sys.exit(run_phase1_report())", phase1_branch)


if __name__ == "__main__":
    unittest.main()
