#!/usr/bin/env python3
"""Focused tests for drift_check.py's issue-fetching (harmonic-forge#220/#223).

Regression coverage for two things found live during the REST migration:
  - `gh issue list` implicitly caps at 30 results (#223); the REST
    `--paginate` replacement must not.
  - The REST `/issues` endpoint also returns PRs, unlike `gh issue list`;
    they must be filtered out or drift_check would flag real PR numbers
    as "issues" missing from the doc.
"""

import importlib.util
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


drift_check = load("drift_check")


class IssueFetchTests(unittest.TestCase):
    def _mock_result(self, numbers_and_pr_flags):
        # `--jq` runs inside the real `gh` process, not in this mock -- this
        # helper can't exercise the jq expression itself (that's confirmed
        # live in the PR description, not by this unit test). What this
        # module CAN and does check: the real command line actually
        # contains the `pull_request == null` filter (test below) and that
        # subprocess.run's parsed stdout is handled correctly once jq has
        # already done its job -- hence pre-filtering here.
        lines = "\n".join(str(n) for n, is_pr in numbers_and_pr_flags if not is_pr)
        return MagicMock(returncode=0, stdout=lines + "\n" if lines else "", stderr="")

    def test_uses_paginate_not_limited_default(self):
        with patch("subprocess.run", return_value=self._mock_result([(1, False)])) as mock_run:
            drift_check.open_issues("vitalharmony/hrse")
        args = mock_run.call_args.args[0]
        self.assertIn("--paginate", args)
        self.assertNotIn("--limit", args)  # no artificial cap reintroduced

    def test_filters_prs_via_jq_expression(self):
        with patch("subprocess.run", return_value=self._mock_result([(1, False)])) as mock_run:
            drift_check.open_issues("vitalharmony/hrse")
        args = mock_run.call_args.args[0]
        jq_expr = args[args.index("--jq") + 1]
        self.assertIn("pull_request == null", jq_expr)

    def test_returns_more_than_30_when_present(self):
        many = self._mock_result([(n, False) for n in range(1, 51)])
        with patch("subprocess.run", return_value=many):
            result = drift_check.open_issues("vitalharmony/hrse")
        self.assertEqual(len(result), 50)

    def test_open_and_closed_use_correct_state_param(self):
        with patch("subprocess.run", return_value=self._mock_result([(1, False)])) as mock_run:
            drift_check.open_issues("vitalharmony/hrse")
            open_args = mock_run.call_args.args[0]
        with patch("subprocess.run", return_value=self._mock_result([(1, False)])) as mock_run:
            drift_check.closed_issues("vitalharmony/hrse")
            closed_args = mock_run.call_args.args[0]
        self.assertIn("state=open", open_args)
        self.assertIn("state=closed", closed_args)


class StrandedWorkTests(unittest.TestCase):
    """hrse#789 — real commits on a branch with no reconciling record."""

    def _ls_remote(self, branch_names):
        lines = "\n".join(f"deadbeef\trefs/heads/{b}" for b in branch_names)
        return MagicMock(returncode=0, stdout=lines + "\n" if lines else "", stderr="")

    def test_branch_naming_convention_matched_and_rejected(self):
        self.assertEqual(drift_check._BRANCH_ISSUE_RE.match("fix/903-hide-cancelled-activities").group(1), "903")
        self.assertEqual(drift_check._BRANCH_ISSUE_RE.match("tooling/892-ontology-doc-split").group(1), "892")
        self.assertIsNone(drift_check._BRANCH_ISSUE_RE.match("main"))
        self.assertIsNone(drift_check._BRANCH_ISSUE_RE.match("903-no-type-prefix"))
        self.assertIsNone(drift_check._BRANCH_ISSUE_RE.match("release-2.7"))

    def test_merged_branch_with_zero_commits_ahead_is_not_reported(self):
        """hrse#789 acceptance criterion 2: a branch already merged into
        main (0 commits ahead) must not be reported."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._ls_remote(["fix/903-hide-cancelled-activities"]),
                MagicMock(returncode=0),  # git fetch origin main
                MagicMock(returncode=0),  # git fetch candidate refs (forge#433)
                MagicMock(returncode=0, stdout="0\n"),  # rev-list --count
            ]
            result = drift_check.branches_ahead_of_main()
        self.assertEqual(result, {})

    def test_branch_ahead_of_main_is_reported_with_count(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._ls_remote(["fix/903-hide-cancelled-activities"]),
                MagicMock(returncode=0),  # git fetch origin main
                MagicMock(returncode=0),  # git fetch candidate refs (forge#433)
                MagicMock(returncode=0, stdout="3\n"),  # rev-list --count
                # forge#433: content is NOT already on main -- three unmatched
                # patches, so the branch survives the patch-id check.
                MagicMock(returncode=0, stdout="+ aaa\n+ bbb\n+ ccc\n"),  # git cherry
            ]
            result = drift_check.branches_ahead_of_main()
        self.assertEqual(result, {903: ("fix/903-hide-cancelled-activities", 3)})

    def test_branch_not_matching_convention_is_invisible(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._ls_remote(["main", "release-2.7", "wip-scratch"]),
                MagicMock(returncode=0),  # git fetch origin main
            ]
            result = drift_check.branches_ahead_of_main()
        self.assertEqual(result, {})

    def test_open_issue_stranded_vs_closed_issue_stranded_are_separated(self):
        ahead = {48: ("fix/48-something", 2), 632: ("fix/632-something", 1)}
        with patch.object(drift_check, "branches_ahead_of_main", return_value=ahead):
            stranded_open, stranded_closed = drift_check.stranded_work(
                open_by_repo={"vitalharmony/hrse": {48}},
                closed_by_repo={"vitalharmony/hrse": {632}},
            )
        self.assertEqual(stranded_open, [(48, "fix/48-something", 2)])
        self.assertEqual(stranded_closed, [(632, "fix/632-something", 1)])

    def test_stale_pr_age_threshold(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        stale = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        rows = f"1\tFresh PR\t{fresh}\n2\tStale PR\t{stale}\n"
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=rows, stderr="")):
            result = drift_check.stale_open_prs("vitalharmony/hrse", stale_days=3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 2)
        self.assertEqual(result[0][1], "Stale PR")


class StaleClosedMentionTests(unittest.TestCase):
    """hrse#917 phase 2 — the first tests this function has ever had.

    That absence is why a positional defect survived five weeks after being
    characterised: phase 1 measured it thoroughly, but nothing pinned the
    production path, so phase 2 could silently not happen.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _doc(self, text: str) -> Path:
        path = self.tmp / "PRIORITIES.md"
        path.write_text(text, encoding="utf-8")
        return path

    def _run(self, text, closed_hrse=(), closed_forge=()):
        rows = drift_check.stale_closed_mentions(
            self._doc(text),
            {"vitalharmony/hrse": set(closed_hrse),
             "vitalharmony/harmonic-forge": set(closed_forge)},
        )
        return sorted({(r.issue, r.repo) for r in rows})

    def test_live_regression_one_issues_closure_must_not_clear_another(self):
        """Criterion 12 — the exact text that defeated the check on 2026-08-16.

        The check reported "No stale-closed mentions found" while the doc
        described closed harmonic-forge#250 as pending work, because the same
        bullet said "closed 2026-08-14" about hrse#802.
        """
        doc = (
            "## In flight\n\n"
            "- **harmonic-forge#250 — `model_tier_gate` full-board scan (3pt).** "
            "The same defect hrse#802 fixed (closed 2026-08-14, resolved by the "
            "Tier migration) in `l1_post`, but in a `PreToolUse` hook that runs "
            "on *every tool call*. Fix is to call #802's existing "
            "`fetch_issue_estimate`.\n"
        )
        flagged = self._run(doc, closed_hrse={802}, closed_forge={250})
        self.assertIn(
            (250, "vitalharmony/harmonic-forge"), flagged,
            "forge#250 is described as pending and is closed -- must flag",
        )

    def test_evidence_bound_to_subject(self):
        """Criterion 2 — a block saying '#855 closed' beside a bare #849."""
        doc = "## S\n\n- hrse#855 closed 2026-08-13. Still to do: hrse#849.\n"
        flagged = self._run(doc, closed_hrse={855, 849})
        self.assertIn((849, "vitalharmony/hrse"), flagged)
        self.assertNotIn((855, "vitalharmony/hrse"), flagged)

    def test_mention_with_its_own_evidence_does_not_flag(self):
        """Criterion 3."""
        doc = "## S\n\n- hrse#855 — closed 2026-08-13, merged in #900.\n"
        self.assertNotIn((855, "vitalharmony/hrse"), self._run(doc, closed_hrse={855}))

    def test_open_issue_is_never_flagged(self):
        doc = "## S\n\n- hrse#999 is in flight.\n"
        self.assertEqual(self._run(doc, closed_hrse={855}), [])

    def test_preamble_is_scanned(self):
        """Criterion 5 — the old rule never saw text before the first bullet,
        which was 9.5 KB including the entire 'In flight' section."""
        doc = "Thesis: hrse#849 is the current focus.\n\n## Later\n\n- nothing\n"
        self.assertIn((849, "vitalharmony/hrse"), self._run(doc, closed_hrse={849}))

    def test_board_links_are_not_issue_mentions(self):
        """Criterion 7."""
        doc = ("## S\n\n- See [hrse #1](https://github.com/users/vitalharmony/"
               "projects/1) for the board.\n")
        self.assertEqual(self._run(doc, closed_hrse={1}), [])

    def test_inserting_an_unrelated_bullet_does_not_change_the_result(self):
        """Criterion 8 — the positional fragility this issue is named for."""
        base = "## S\n\n- hrse#855 closed 2026-08-13.\n- Still to do: hrse#849.\n"
        inserted = ("## S\n\n- hrse#855 closed 2026-08-13.\n"
                    "- An unrelated new bullet about nothing.\n"
                    "- Still to do: hrse#849.\n")
        self.assertEqual(
            self._run(base, closed_hrse={855, 849}),
            self._run(inserted, closed_hrse={855, 849}),
        )

    def test_fallback_matches_the_documented_degraded_behaviour(self):
        """The import-failure path stays callable, so a broken sibling
        degrades this check rather than crashing the sweep."""
        doc = "## S\n\n- hrse#855 closed. Still to do: hrse#849.\n"
        out = drift_check._stale_closed_mentions_block_scoped(
            self._doc(doc),
            {"vitalharmony/hrse": {855, 849}, "vitalharmony/harmonic-forge": set()},
        )
        self.assertEqual(out, [], "the old rule clears the whole block -- that is the bug")


class AdjudicatedFixtures(unittest.TestCase):
    """Cases named by the hrse#917 pitch-inspection, pinned so no future
    scoping or aggregation change can quietly suppress them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self, text, closed_hrse=()):
        path = self.tmp / "PRIORITIES.md"
        path.write_text(text, encoding="utf-8")
        return drift_check.stale_closed_mentions(
            path,
            {"vitalharmony/hrse": set(closed_hrse),
             "vitalharmony/harmonic-forge": set()},
        )

    def _issues(self, *a, **kw):
        return {r.issue for r in self._rows(*a, **kw)}

    def test_860_trap_pre_926_text_still_flags(self):
        """The mandatory elimination gate, as a synthetic.

        PR #926 rewrote the live doc to '**#860 - closed 2026-08-14**', so
        #860 now legitimately clears and the trap cannot be asserted against
        the current document. Text quoted from `52fe281:docs/PRIORITIES.md`
        (:276, :289) -- the state that actually defeated the check.

        A document-wide 'clear if any mention clears' rule fails here: the
        neighbouring `merged` belongs to other issues.
        """
        doc = (
            "## Dependencies that bite\n\n"
            "- caught by hand). **#860 remains open** (outstanding work from any "
            "merge gets a real tracker).\n"
            "- **#850** and **#871**/**#874** closed in the same chain. With those "
            "merged, the migration-control thread is complete except **#860**.\n"
        )
        self.assertIn(860, self._issues(doc, closed_hrse={860, 850, 871, 874}))

    def test_858_described_as_not_yet_merged(self):
        """Live shape at PRIORITIES.md:255-257. Clear-if-any-mention-clears
        suppressed this one entirely -- #858's only other mention was
        role-classified a citation, which abstains rather than clears."""
        doc = (
            "## Dependencies that bite\n\n"
            "- **Dependency that bites: hrse#858 is ahead of the #856/#879 unit "
            "and overlaps it** on `CLAUDE.md`. The combined branch rebases onto "
            "`main` after #858 merges, then re-runs its full gate.\n"
        )
        self.assertIn(858, self._issues(doc, closed_hrse={858}))

    def test_stale_sequencing_paragraph(self):
        """Live shape at PRIORITIES.md:455-464 -- four closed issues inside one
        forward-looking sequencing claim."""
        doc = (
            "## Queued behind the thesis\n\n"
            "- Sequence: **#855** (fold `ActionItem` into one `Task` label) -> "
            "**#856** (hybrid follow-up surface). #856 is gated on #855 alone. "
            "**#253 closes with #855**, and **#852** is largely absorbed by the "
            "two together.\n"
        )
        flagged = self._issues(doc, closed_hrse={855, 856, 253, 852})
        for n in (855, 856, 852):
            self.assertIn(n, flagged)

    def test_conjoined_subject_shares_the_predicate(self):
        """'A and B both merged and closed' is honest prose, not drift.

        An evidence-binding refinement, not aggregation: the intervening
        number shares the predicate. Contrast the #860 trap above, where
        prose sits between the token and the mention.
        """
        doc = ("## In flight\n\n"
               "- hrse#792 and hrse#793 both merged and closed 2026-08-13.\n")
        self.assertEqual(self._issues(doc, closed_hrse={792, 793}), set())

    def test_citations_abstain_they_do_not_clear(self):
        """A citation asserts no status, so it cannot be stale -- but it must
        not vouch for a different, genuinely stale mention either."""
        doc = ("## S\n\n"
               "- the same divergence class the #849 chain was built to eliminate.\n"
               "- Still to do: hrse#849 ships the sweep.\n")
        self.assertIn(849, self._issues(doc, closed_hrse={849}))

    def test_rows_carry_line_and_context(self):
        """Per-mention reporting is the point: a bare issue number is not
        actionable, a file:line with context is."""
        doc = "## S\n\n- Still to do: hrse#849 ships the sweep.\n"
        rows = self._rows(doc, closed_hrse={849})
        self.assertTrue(rows)
        self.assertEqual(rows[0].issue, 849)
        self.assertGreater(rows[0].line, 0)
        self.assertIn("#849", rows[0].context)


class CutsDocScanningTests(unittest.TestCase):
    """hrse#974 — PRIORITIES-cuts.md is checked; PRIORITIES-archive.md is not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _cuts(self, text):
        path = self.tmp / "PRIORITIES-cuts.md"
        path.write_text(text, encoding="utf-8")
        return drift_check.stale_closed_mentions(
            path, {"vitalharmony/hrse": {246, 220},
                   "vitalharmony/harmonic-forge": {246, 220}})

    def test_a_resolved_cut_is_flagged(self):
        """The live instance this issue was filed for: forge#246 sat in
        cuts.md as a deferral after it closed, and the check said clean."""
        doc = ("## Cuts\n\n"
               "- **harmonic-forge#246 (board-add fails) — filed 2026-08-12.** "
               "Tooling bug; not pipeline-critical — LATER.\n")
        self.assertTrue(any(r.issue == 246 for r in self._cuts(doc)))

    def test_a_citation_inside_a_cut_is_not_flagged(self):
        """Subject-scoped: only the entry's own subject is a claim about it.

        Trailing references are supporting prose. Measured live, scanning
        every mention gave 107 rows dominated by exactly this shape.
        """
        doc = ("## Cuts\n\n"
               "- **hrse#999 (something open) — filed.** Found during "
               "harmonic-forge#220's review; pre-existing. LATER.\n")
        self.assertEqual([r.issue for r in self._cuts(doc)], [])

    def test_a_cut_disclosing_its_own_closure_is_not_flagged(self):
        doc = ("## Cuts\n\n"
               "- **harmonic-forge#246 — closed 2026-08-15, no longer a cut.** "
               "Resolved incidentally.\n")
        self.assertEqual([r.issue for r in self._cuts(doc)], [])

    def test_archive_is_excluded_by_configuration(self):
        """Not merely unscanned by accident -- asserted, because scanning it
        would report ~184 findings on day one and make the check ignorable."""
        names = [p.name for p in drift_check.CHECKED_DOCS]
        self.assertIn("PRIORITIES.md", names)
        self.assertIn("PRIORITIES-cuts.md", names)
        self.assertNotIn("PRIORITIES-archive.md", names)
        self.assertIn("PRIORITIES-archive.md",
                      [p.name for p in drift_check.UNCHECKED_DOCS])


class DocResolvedMarkerTests(unittest.TestCase):
    """hrse#974 triage — three markers the doc uses that mention-level
    scoping read straight past. Each produced false reports."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _cuts(self, text):
        path = self.tmp / "PRIORITIES-cuts.md"
        path.write_text(text, encoding="utf-8")
        return [r.issue for r in drift_check.stale_closed_mentions(
            path, {"vitalharmony/hrse": {246, 324, 803, 404},
                   "vitalharmony/harmonic-forge": {246, 324, 803, 404}})]

    def _live(self, text):
        path = self.tmp / "PRIORITIES.md"
        path.write_text(text, encoding="utf-8")
        return [r.issue for r in drift_check.stale_closed_mentions(
            path, {"vitalharmony/hrse": {849}, "vitalharmony/harmonic-forge": set()})]

    def test_strikethrough_marks_an_entry_resolved(self):
        doc = ("## Cuts\n\n"
               "- ~~**harmonic-forge#246 (board-add fails) — filed.** LATER.~~ "
               "No longer a cut.\n")
        self.assertEqual(self._cuts(doc), [])

    def test_promoted_to_is_a_departure_like_closed(self):
        doc = ("## Cuts\n\n"
               "- **hrse#324 — promoted to NOW item 39, 2026-07-30.** See there.\n")
        self.assertEqual(self._cuts(doc), [])

    def test_a_retrospective_section_heading_clears_its_mentions(self):
        """The highest-value signal, and it serves both documents."""
        doc = ("## Settled 2026-08-13/14 — migrations can no longer be closed unrun\n\n"
               "- hrse#849 denies closing a data-migration issue unlabelled.\n")
        self.assertEqual(self._live(doc), [])

    def test_the_same_text_outside_a_retrospective_section_still_flags(self):
        """Guards the section rule from clearing everything."""
        doc = ("## In flight\n\n"
               "- hrse#849 denies closing a data-migration issue unlabelled.\n")
        self.assertIn(849, self._live(doc))

    def test_a_bold_title_stating_closure_clears_the_subject(self):
        """C6's 20-char window cannot reach the end of a long title."""
        doc = ("## Cuts\n\n"
               "- **hrse#803 — cost-aware Projects v2 enforcement, closed "
               "2026-08-13 without being built.** Parked earlier.\n")
        self.assertEqual(self._cuts(doc), [])

    def test_a_closure_in_the_BODY_does_not_clear_the_subject(self):
        """Measured: clearing on any closure word in the bullet produced four
        false negatives. A body citing another thing's merge says nothing
        about this entry's subject."""
        doc = ("## Cuts\n\n"
               "- **hrse#404 — implemented 2026-07-27/28, verify then close.**\n"
               "  rule 9 (live-preview provisioning), merged harmonic-forge PR#122.\n")
        self.assertIn(404, self._cuts(doc))


if __name__ == "__main__":
    unittest.main()


class Phase4CutsSubjectSpan(unittest.TestCase):
    """hrse#917 phase 4 — the subject is the first ref in the BOLD TITLE."""

    def _cuts(self, body, closed_hrse):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "PRIORITIES-cuts.md"
            p.write_text("## LATER — explicit cuts\n\n" + body, encoding="utf-8")
            rows = drift_check.stale_closed_mentions(
                p, {"vitalharmony/hrse": set(closed_hrse)})
            return {r.issue for r in rows}

    def test_title_carries_the_subject_not_the_body(self):
        """The live false positive: an entry whose subject is a DECISION, with
        a closed issue cited in its prose. Must not flag."""
        body = ("- **Follow-up model — commitments vs. waits. Deliberately NOT "
                "filed as work, 2026-08-13.** The observable half is filed as "
                "hrse#852 (recorded three ways).\n")
        self.assertEqual(self._cuts(body, {852}), set())

    def test_title_ref_is_still_the_subject(self):
        body = ("- **hrse#803 — cost-aware enforcement.** Superseded by "
                "hrse#900 which remains open.\n")
        self.assertEqual(self._cuts(body, {803}), {803})

    def test_body_ref_is_never_promoted_to_subject(self):
        """Title issue OPEN, body issue CLOSED -> no flag. Previously flagged."""
        body = ("- **hrse#900 — still a live cut.** Found during hrse#803's "
                "review.\n")
        self.assertEqual(self._cuts(body, {803}), set())

    def test_a_category_heading_entry_does_not_flag_its_list(self):
        body = ("- **Duplicate (1)** — #884, filed the same day as a duplicate "
                "of #767.\n")
        self.assertEqual(self._cuts(body, {884, 767}), set())

    def test_bullet_with_no_bold_title_abstains(self):
        body = "- hrse#803 with no bolded title at all.\n"
        self.assertEqual(self._cuts(body, {803}), set())

    def test_a_struck_title_still_resolves(self):
        body = "- ~~**hrse#803 — withdrawn.**~~ Struck through.\n"
        self.assertEqual(self._cuts(body, {803}), set())

    def test_body_closure_does_not_clear_a_title_subject(self):
        """The inverted-bold-span regression. `merged` sits in the BODY and
        belongs to another issue; it must not clear the title's subject."""
        body = ("- **hrse#803 — cost-aware enforcement.** The related "
                "harmonic-forge PR was merged last week.\n")
        self.assertEqual(self._cuts(body, {803}), {803})


class Phase3CitationShape(unittest.TestCase):
    """hrse#917 phase 3 — structural citation marks, orthogonal to evidence."""

    def _flag(self, body, closed_hrse):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "PRIORITIES.md"
            p.write_text(body, encoding="utf-8")
            rows = drift_check.stale_closed_mentions(
                p, {"vitalharmony/hrse": set(closed_hrse)})
            return {r.issue for r in rows}

    def test_a_backticked_mention_is_a_citation(self):
        doc = ("## Tooling\n\n- Measured against two canaries: `#848`'s "
               "declaration and `#860`'s three drift claims.\n")
        self.assertEqual(self._flag(doc, {848, 860}), set())

    def test_a_parenthetical_mention_is_a_citation(self):
        doc = ("## In flight\n\n- The milestone now carries membership "
               "(hrse#283); this doc no longer names issues.\n")
        self.assertEqual(self._flag(doc, {283}), set())

    def test_a_parenthetical_wrapped_across_lines_is_still_a_citation(self):
        """Line-scoped detection misses every wrapped parenthetical."""
        doc = ("## In flight\n\n- Both merged and closed 2026-08-13 (#792 "
               "ranks the queue by\n  warm path; #793 replaced the filter).\n")
        self.assertEqual(self._flag(doc, {792, 793}), set())

    def test_a_quoted_mention_is_a_citation(self):
        doc = ('## In flight\n\n- Justified as "a correctness bug for #792, '
               'not cosmetic cleanup." A review falsified that.\n')
        self.assertEqual(self._flag(doc, {792}), set())

    def test_a_retrospective_heading_clears_its_section(self):
        doc = "## Settled 2026-08-14\n\n- **hrse#803** was cut here.\n"
        self.assertEqual(self._flag(doc, {803}), set())

    def test_dependencies_that_bite_is_NOT_treated_as_retrospective(self):
        """Load-bearing. That section carries the most flags AND both
        adversarial canaries. Blanket-clearing it is the false-clean failure
        phases 1-2 exist to prevent."""
        doc = ("## Dependencies that bite\n\n- **#860 remains open** "
               "(outstanding work gets a real tracker).\n")
        self.assertEqual(self._flag(doc, {860}), {860})

    def test_a_bare_claim_in_a_live_section_still_flags(self):
        doc = "## In flight\n\n- **hrse#803** is the next thing to build.\n"
        self.assertEqual(self._flag(doc, {803}), {803})


class BaselineSelfExpiry(unittest.TestCase):
    """hrse#917 — a suppression that outlives its subject is how a baseline
    rots into a blanket bypass. Every one of these must be an ERROR."""

    ENTRY = {"doc": "PRIORITIES.md", "repo": "vitalharmony/hrse", "issue": 903,
             "quote": "after #903", "why": "temporal citation",
             "reviewed": "2026-08-17"}

    def _row(self, issue=903, context="in as many days after #903, both"):
        return drift_check.StaleMention(
            issue=issue, repo="vitalharmony/hrse", line=117, context=context)

    def test_a_matching_entry_suppresses_the_row(self):
        kept, used = drift_check.apply_baseline(
            "PRIORITIES.md", [self._row()], [self.ENTRY])
        self.assertEqual(kept, [])
        self.assertEqual(used, [self.ENTRY])

    def test_a_quote_that_no_longer_appears_does_not_suppress(self):
        """The prose was edited -> the entry stops applying, and (below) is
        then reported as stale rather than silently doing nothing."""
        kept, used = drift_check.apply_baseline(
            "PRIORITIES.md", [self._row(context="rewritten sentence")],
            [self.ENTRY])
        self.assertEqual(len(kept), 1)
        self.assertEqual(used, [])

    def test_an_entry_matching_nothing_is_an_error(self):
        problems = drift_check.validate_baseline([self.ENTRY], [], {})
        self.assertTrue(problems)
        self.assertIn("matches no flagged mention", problems[0])

    def test_an_entry_whose_issue_reopened_is_an_error(self):
        problems = drift_check.validate_baseline(
            [self.ENTRY], [self.ENTRY], {"vitalharmony/hrse": {903}})
        self.assertTrue(any("OPEN" in p for p in problems))

    def test_a_live_entry_is_not_an_error(self):
        self.assertEqual(
            drift_check.validate_baseline([self.ENTRY], [self.ENTRY], {}), [])

    def test_the_wrong_doc_does_not_suppress(self):
        entry = dict(self.ENTRY, doc="PRIORITIES-cuts.md")
        kept, _ = drift_check.apply_baseline(
            "PRIORITIES.md", [self._row()], [entry])
        self.assertEqual(len(kept), 1)

    def test_the_wrong_issue_does_not_suppress(self):
        kept, _ = drift_check.apply_baseline(
            "PRIORITIES.md", [self._row(issue=904)], [self.ENTRY])
        self.assertEqual(len(kept), 1)

    def test_an_entry_missing_why_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.toml"
            p.write_text('[[citation]]\ndoc="PRIORITIES.md"\n'
                         'repo="vitalharmony/hrse"\nissue=903\n'
                         'quote="x"\nreviewed="2026-08-17"\n')
            with self.assertRaises(drift_check.BaselineError) as cm:
                drift_check.load_baseline(p)
            self.assertIn("why", str(cm.exception))

    def test_a_missing_baseline_file_is_not_an_error(self):
        self.assertEqual(drift_check.load_baseline(Path("/nonexistent.toml")), [])

    def test_the_shipped_baseline_parses_and_is_fully_specified(self):
        entries = drift_check.load_baseline()
        self.assertTrue(entries, "the shipped baseline should be non-empty")
        for e in entries:
            for k in ("doc", "repo", "issue", "quote", "why", "reviewed"):
                self.assertTrue(e.get(k), f"{e} missing {k}")


class PatchIdStrandedTests(unittest.TestCase):
    """harmonic-forge#433 — squash-merge must not read as lost work.

    These build **real git repositories** in a temp dir rather than mocking
    `subprocess.run`. The defect is entirely about what git actually reports
    for a given history shape, so a mock would only assert my own beliefs
    about `git cherry` — and one of those beliefs (that a naive merge-commit
    branch reproduces the false negative) turned out to be wrong. No branch is
    ever pushed to a real remote; that is what created the 14 false positives
    this issue fixes.
    """

    def _repo(self):
        import subprocess as sp
        d = tempfile.mkdtemp()
        run = lambda *a: sp.run(a, cwd=d, capture_output=True, text=True, check=True)
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        Path(d, "f").write_text("base\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "base")
        return d, run

    def _cherry_clean(self, d, branch):
        """Mirror `_content_is_already_on_main`, but against local refs."""
        import subprocess as sp
        cherry = sp.run(["git", "cherry", "main", branch],
                        cwd=d, capture_output=True, text=True)
        unmatched = [ln for ln in cherry.stdout.splitlines() if ln.startswith("+")]
        merges = sp.run(["git", "rev-list", "--count", "--merges", f"main..{branch}"],
                        cwd=d, capture_output=True, text=True)
        return not unmatched and int(merges.stdout.strip() or 0) == 0

    def test_one_to_one_squash_is_suppressed_by_patch_id_alone(self):
        d, run = self._repo()
        run("git", "checkout", "-qb", "one")
        Path(d, "f").write_text("base\na\n")
        run("git", "commit", "-qam", "commit A")
        run("git", "checkout", "-q", "main")
        run("git", "merge", "-q", "--squash", "one")
        run("git", "commit", "-qm", "squashed A (#1)")
        self.assertTrue(self._cherry_clean(d, "one"),
                        "a 1:1 squash must be recognised by patch-id")

    def test_n_to_one_squash_is_NOT_caught_by_patch_id(self):
        """The case that separates a real fix from a partial one.

        An N->1 squash produces no matching patch-id for any individual
        commit, so `git cherry` still reports `+`. 4 of the 14 false positives
        were this shape, and only the merged-PR signal resolves them.
        """
        d, run = self._repo()
        run("git", "checkout", "-qb", "many")
        Path(d, "g").write_text("x\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "part 1")
        Path(d, "g").write_text("x\ny\n")
        run("git", "commit", "-qam", "part 2")
        run("git", "checkout", "-q", "main")
        run("git", "merge", "-q", "--squash", "many")
        run("git", "commit", "-qm", "squashed many (#2)")
        self.assertFalse(self._cherry_clean(d, "many"),
                         "patch-id alone must NOT be trusted to clear an N:1 squash")

    def test_genuinely_unmerged_branch_is_reported(self):
        d, run = self._repo()
        run("git", "checkout", "-qb", "real")
        Path(d, "h").write_text("z\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "genuinely unmerged")
        run("git", "checkout", "-q", "main")
        self.assertFalse(self._cherry_clean(d, "real"))

    def test_merge_commit_carrying_unique_content_is_not_cleared(self):
        """The false negative — the direction that loses work.

        `git cherry` ignores merge commits, so a branch whose only commit
        ahead is a merge carrying resolution-only content reports zero
        unmatched patches while that content is absent from main. The
        merge-count guard is what stops that reading as "content present".
        """
        import subprocess as sp
        d, run = self._repo()
        run("git", "checkout", "-qb", "side")
        Path(d, "s").write_text("s\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "side work")
        run("git", "checkout", "-q", "main")
        run("git", "merge", "-q", "--no-ff", "side", "-m", "merge side")
        head = sp.run(["git", "rev-parse", "HEAD"], cwd=d,
                      capture_output=True, text=True).stdout.strip()
        side = sp.run(["git", "rev-parse", "side"], cwd=d,
                      capture_output=True, text=True).stdout.strip()
        Path(d, "evil").write_text("evil\n")
        run("git", "add", "evil")
        tree = sp.run(["git", "write-tree"], cwd=d,
                      capture_output=True, text=True).stdout.strip()
        merge = sp.run(["git", "commit-tree", tree, "-p", head, "-p", side,
                        "-m", "evil merge"], cwd=d,
                       capture_output=True, text=True).stdout.strip()
        run("git", "branch", "feat", merge)
        run("git", "reset", "-q", "--hard", head)

        cherry = sp.run(["git", "cherry", "main", "feat"], cwd=d,
                        capture_output=True, text=True).stdout
        self.assertEqual([ln for ln in cherry.splitlines() if ln.startswith("+")], [],
                         "precondition: git cherry must report nothing here")
        self.assertFalse(
            self._cherry_clean(d, "feat"),
            "cherry's silence on a merge commit must not clear the branch")


class MergedPrSignalTests(unittest.TestCase):
    """harmonic-forge#433 — the second signal, and how it must fail."""

    def test_merged_head_refs_are_parsed(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="fix/1-a\tsha1\nfeat/2-b\tsha2\n", stderr="")
            refs = drift_check.merged_pr_head_refs("vitalharmony/hrse")
        self.assertEqual(set(refs), {"fix/1-a", "feat/2-b"})
        args = mock_run.call_args[0][0]
        self.assertIn("state=all", args)
        self.assertNotIn("pr", args[:2])  # never `gh pr list` (GraphQL-backed)

    def test_a_failed_lookup_raises_rather_than_reporting_nothing_merged(self):
        """Failing open would resurrect every squash-merged branch as a false
        positive — the whole defect. An empty set is indistinguishable from a
        real answer, so it must raise."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(RuntimeError):
                drift_check.merged_pr_head_refs("vitalharmony/hrse")

    def test_branch_with_merged_pr_is_dropped_from_both_directions(self):
        """The N->1 squash case: the merged PR accounts for everything on the
        branch, so nothing remains beyond it."""
        ahead = {195: ("feat/195-sensing-layer", 1), 48: ("fix/48-x", 2)}
        with patch.object(drift_check, "branches_ahead_of_main", return_value=ahead), \
             patch.object(drift_check, "unmatched_beyond", return_value=False), \
             patch.object(drift_check, "merged_pr_head_refs",
                          return_value={"feat/195-sensing-layer": "abc123"}):
            stranded_open, stranded_closed = drift_check.stranded_work(
                open_by_repo={"vitalharmony/hrse": {48}},
                closed_by_repo={"vitalharmony/hrse": {195}},
            )
        self.assertEqual(stranded_open, [(48, "fix/48-x", 2)])
        self.assertEqual(stranded_closed, [],
                         "#195 had merged PR #1452; it must not be reported")

    def test_a_merged_pr_does_not_override_unmatched_patches_beyond_it(self):
        """The sibling the shipped drop-test needed (harmonic-forge#433 FAIL).

        `test_branch_with_merged_pr_is_dropped_from_both_directions` gives its
        merged branch nothing beyond the merge and asserts the drop is
        correct — it DOCUMENTS the suppression rather than bounding it. This
        bounds it: work pushed after the merge is genuinely absent from main,
        and a merged PR must not explain it away.

        Same asymmetry the merge-commit guard uses: over-report rather than
        lose work.
        """
        ahead = {195: ("feat/195-sensing-layer", 2)}
        with patch.object(drift_check, "branches_ahead_of_main", return_value=ahead), \
             patch.object(drift_check, "unmatched_beyond", return_value=True), \
             patch.object(drift_check, "merged_pr_head_refs",
                          return_value={"feat/195-sensing-layer": "abc123"}):
            stranded_open, stranded_closed = drift_check.stranded_work(
                open_by_repo={"vitalharmony/hrse": {195}},
                closed_by_repo={"vitalharmony/hrse": set()},
            )
        self.assertEqual(stranded_open, [(195, "feat/195-sensing-layer", 2)],
                         "unmatched patches beyond a merged PR must still report")

    def test_unmatched_beyond_fails_toward_reporting(self):
        """An unknowable answer must never silently clear a branch."""
        self.assertTrue(drift_check.unmatched_beyond("any/branch", ""))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            self.assertTrue(drift_check.unmatched_beyond("any/branch", "deadbeef"))

    def test_merged_pr_map_carries_the_head_sha(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="fix/1-a\tsha1\nfeat/2-b\tsha2\n", stderr="")
            merged = drift_check.merged_pr_head_refs("vitalharmony/hrse")
        self.assertEqual(merged, {"fix/1-a": "sha1", "feat/2-b": "sha2"})


class ConfigRefusalTests(unittest.TestCase):
    """harmonic-forge#708 preclose finding: an unresolvable config must refuse,
    never fall through to "No stale-closed mentions" having checked nothing."""

    def test_main_refuses_without_a_config(self) -> None:
        err = drift_check._home.config_loader.ConfigError("no config")
        with patch.object(drift_check._home, "repo_names", side_effect=err), \
             patch.object(drift_check, "closed_issues") as closed, \
             patch("sys.stderr"):
            self.assertEqual(drift_check.main(), 2)
        closed.assert_not_called()

    def test_main_refuses_an_empty_repo_group(self) -> None:
        with patch.object(drift_check._home, "repo_names", return_value=[]), \
             patch.object(drift_check, "closed_issues") as closed, \
             patch("sys.stderr"):
            self.assertEqual(drift_check.main(), 2)
        closed.assert_not_called()
